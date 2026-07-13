package main

import (
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"time"
)

type recentRecordMessage struct {
	GroupId   string
	SelfID    string
	UserID    string
	Nickname  string
	MessageId string
	SeenAt    time.Time
}

var (
	recentRecordMu      sync.Mutex
	recentRecord        recentRecordMessage
	voiceTranscriptSeen sync.Map // group|message|text -> unix nano
)

func rememberRecentRecordMessage(m *WechatMessage) {
	if m == nil || m.GroupId == "" || m.MessageType != "group" {
		return
	}
	for _, msg := range m.Message {
		if msg != nil && msg.Type == "record" {
			nick := ""
			if m.Sender != nil {
				nick = m.Sender.Nickname
			}
			recentRecordMu.Lock()
			recentRecord = recentRecordMessage{
				GroupId:   m.GroupId,
				SelfID:    m.SelfID,
				UserID:    m.UserID,
				Nickname:  nick,
				MessageId: m.MessageId,
				SeenAt:    time.Now(),
			}
			recentRecordMu.Unlock()
			Info("记录最近语音泡，等待 UI 转文字", "group_id", m.GroupId, "message_id", m.MessageId, "sender", nick)
			return
		}
	}
}

func cleanTranscriptText(text string) string {
	text = strings.TrimSpace(strings.ReplaceAll(text, "\u00a0", " "))
	text = regexp.MustCompile(`\s+`).ReplaceAllString(text, " ")
	text = strings.Trim(text, " \t\r\n\"'“”‘’")
	return text
}

func isLikelyVoiceTranscript(text string) bool {
	text = cleanTranscriptText(text)
	if len([]rune(text)) < 4 || len([]rune(text)) > 160 {
		return false
	}
	if !regexp.MustCompile(`[\p{Han}]`).MatchString(text) {
		return false
	}
	noise := []string{
		"语音输入", "按住说话", "松开发送", "转文字", "语音转文字", "在群聊中发了一段语音",
		"图片/语音图库", "发送", "聊天记录", "文件传输助手", "值班群", "扯淡群", "微信", "WeChat",
		"系统设置", "辅助功能", "朋友圈", "通讯录", "收藏", "搜索",
	}
	for _, n := range noise {
		if text == n || strings.Contains(text, n) && len([]rune(text)) <= len([]rune(n))+4 {
			return false
		}
	}
	if strings.Contains(text, "@chatroom") || strings.Contains(text, "wxid_") || strings.Contains(text, "file_id=") {
		return false
	}
	return true
}

func handleVoiceTranscriptUICandidate(pMap map[string]interface{}) {
	text, _ := pMap["text"].(string)
	text = cleanTranscriptText(text)
	if !isLikelyVoiceTranscript(text) {
		return
	}
	recentRecordMu.Lock()
	rec := recentRecord
	recentRecordMu.Unlock()
	if rec.GroupId == "" || time.Since(rec.SeenAt) > 45*time.Second {
		return
	}
	key := rec.GroupId + "|" + rec.MessageId + "|" + text
	if _, loaded := voiceTranscriptSeen.LoadOrStore(key, time.Now().UnixNano()); loaded {
		return
	}
	recentRecordMu.Lock()
	if recentRecord.MessageId == rec.MessageId {
		recentRecord = recentRecordMessage{}
	}
	recentRecordMu.Unlock()
	Info("捕获微信 UI 语音转文字", "group_id", rec.GroupId, "message_id", rec.MessageId, "text", text)
	msg := &WechatMessage{
		GroupId:             rec.GroupId,
		SelfID:              rec.SelfID,
		UserID:              rec.UserID,
		Sender:              &Sender{UserID: rec.UserID, Nickname: rec.Nickname},
		Time:                time.Now().UnixMilli(),
		PostType:            "message",
		MessageId:           rec.MessageId + "-voice-transcript",
		Message:             []*Message{{Type: "text", Data: &SendRequestData{Text: "[语音转文字] " + text}}},
		RawMessage:          "[语音转文字] " + text,
		ShowContent:         text,
		MessageType:         "group",
		VoiceTranscript:     true,
		VoiceTranscriptText: text,
		VoiceMessageId:      rec.MessageId,
	}
	jsonReq, err := json.Marshal(msg)
	if err != nil {
		Error("语音转文字事件序列化失败", "err", err)
		return
	}
	if config.ConnType == "http" {
		go SendHttpReq(jsonReq)
	} else {
		go SendWebSocketMsg(jsonReq)
	}
}
