package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func HandleProtobufMsgAndSend(payload map[string]interface{}) {
	jsonList, err := HandleProtobufMsg(payload)
	if err != nil {
		if isUnsupportedProtobufMessage(err) {
			Warn("跳过不匹配的protobuf消息", "err", err)
			return
		}
		Error("protobuf消息处理失败", "err", err)
		return
	}

	// 一个数据帧可能打包了多条消息，逐条下发
	for _, jsonData := range jsonList {
		if jsonData == nil {
			continue
		}
		if config.ConnType == "http" {
			SendHttpReq(jsonData)
		} else {
			SendWebSocketMsg(jsonData)
		}
	}
}

func isUnsupportedProtobufMessage(err error) bool {
	if err == nil {
		return false
	}

	message := err.Error()
	return strings.Contains(message, "cannot parse invalid wire-format data") ||
		strings.Contains(message, "cannot extract message data") ||
		strings.Contains(message, "missing required fields") ||
		strings.Contains(message, "no messages found")
}

// consumeBytesFields 从一段 protobuf 原始字节里，取出指定字段号、且 wiretype 为
// bytes(2) 的所有值。用于遍历"重复出现的 singular 字段"（proto.Unmarshal 只会保留
// 最后一个，这里手动全部取出）。
func consumeBytesFields(raw []byte, field protowire.Number) [][]byte {
	var out [][]byte
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 {
			break
		}
		raw = raw[n:]
		if typ == protowire.BytesType {
			v, m := protowire.ConsumeBytes(raw)
			if m < 0 {
				break
			}
			if num == field {
				out = append(out, v)
			}
			raw = raw[m:]
			continue
		}
		m := protowire.ConsumeFieldValue(num, typ, raw)
		if m < 0 {
			break
		}
		raw = raw[m:]
	}
	return out
}

// parseAllRecvData 从 WxRecvMsg 原始字节里提取所有消息数据。
// 结构: WxRecvMsg.wrapper(field2) -> wrapper.body(field2, 可重复) -> body.content.data
// 一个 wrapper 里可能打包多条 body，需要全部取出。
func parseAllRecvData(rawBytes []byte) []*wxproto.WxRecvMsgData {
	var result []*wxproto.WxRecvMsgData
	for _, wrapperRaw := range consumeBytesFields(rawBytes, 2) {
		for _, bodyRaw := range consumeBytesFields(wrapperRaw, 2) {
			body := &wxproto.WxRecvMsgBody{}
			if err := proto.Unmarshal(bodyRaw, body); err != nil {
				continue
			}
			if body.Content != nil && body.Content.Data != nil {
				result = append(result, body.Content.Data)
			}
		}
	}
	return result
}

func HandleProtobufMsg(payload map[string]interface{}) ([][]byte, error) {
	dataInter, ok := payload["data"]
	if !ok {
		return nil, fmt.Errorf("protobuf_msg: missing data field")
	}

	dataArr, ok := dataInter.([]interface{})
	if !ok {
		return nil, fmt.Errorf("protobuf_msg: data is not array")
	}

	rawBytes := make([]byte, len(dataArr))
	for i, v := range dataArr {
		num, ok := v.(float64)
		if !ok {
			return nil, fmt.Errorf("protobuf_msg: data[%d] is not number", i)
		}
		rawBytes[i] = byte(int(num))
	}

	//fmt.Println("[receive protobuf data]", HexDump(rawBytes, 0))

	dataList := parseAllRecvData(rawBytes)
	if len(dataList) == 0 {
		return nil, fmt.Errorf("protobuf_msg: cannot extract message data")
	}

	var jsonList [][]byte
	for _, data := range dataList {
		jsonData, err := buildWechatMessageJSON(data)
		if err != nil {
			// 单条消息（如系统通知/字段不全）不阻断其他消息
			Warn("跳过单条protobuf消息", "err", err)
			continue
		}
		if jsonData != nil {
			jsonList = append(jsonList, jsonData)
		}
	}

	if len(jsonList) == 0 {
		return nil, fmt.Errorf("protobuf_msg: no messages found")
	}

	return jsonList, nil
}

// buildWechatMessageJSON 把单条 WxRecvMsgData 转成 WechatMessage JSON。
func buildWechatMessageJSON(data *wxproto.WxRecvMsgData) ([]byte, error) {
	sender := ""
	receiver := ""
	content := ""
	if data.Sender != nil {
		sender = data.Sender.Value
	}
	if data.Receiver != nil {
		receiver = data.Receiver.Value
	}
	if data.Content != nil {
		content = data.Content.Value
	}
	xmlStr := string(data.Xml)
	userContent := string(data.UserContent)
	msgId := fmt.Sprintf("%d", data.MsgId)

	if sender == "" || receiver == "" || content == "" || msgId == "" || msgId == "0" {
		return nil, fmt.Errorf("protobuf_msg: missing required fields sender=%s receiver=%s content_len=%d msgId=%s",
			sender, receiver, len(content), msgId)
	}

	selfId := receiver
	if strings.Contains(receiver, "@chatroom") {
		selfId = sender
	}
	msgType := "private"
	groupId := ""
	senderUser := sender
	senderNickname := ""
	messages := getMessagesFromProto(content, sender, data.MediaContent)
	if len(messages) == 0 {
		return nil, fmt.Errorf("protobuf_msg: no messages found")
	}

	if strings.Contains(sender, "@chatroom") {
		msgType = "group"
		groupId = sender

		splitIndex := strings.Index(content, ":")
		sendUserStart := strings.Index(content, "wxid_")
		if sendUserStart >= 0 && splitIndex > sendUserStart {
			senderUser = strings.TrimSpace(content[sendUserStart:splitIndex])
		}

		atUserMatch := regexp.MustCompile(`<atuserlist>([\s\S]*?)</atuserlist>`).FindStringSubmatch(xmlStr)
		if len(atUserMatch) > 1 {
			atUsers := strings.Split(atUserMatch[1], ",")
			for _, atUser := range atUsers {
				atUser = strings.TrimSpace(atUser)
				if atUser != "" {
					messages = append(messages, &Message{Type: "at", Data: &SendRequestData{QQ: atUser}})
				}
			}
		}

		// 处理用户的名称
		splitIdx := strings.Index(userContent, ":")
		if splitIdx == -1 {
			if idx := strings.Index(userContent, "在群聊中@了你"); idx != -1 {
				senderNickname = strings.TrimSpace(userContent[:idx])
			} else if idx := strings.Index(userContent, "在群聊中发了一段语"); idx != -1 {
				senderNickname = strings.TrimSpace(userContent[:idx])
			}
		} else {
			senderNickname = strings.TrimSpace(userContent[:splitIdx])
		}
		if senderNickname == "" {
			senderNickname = senderUser
		}
	} else {
		splitIdx := strings.Index(userContent, ":")
		if splitIdx != -1 {
			senderNickname = strings.TrimSpace(userContent[:splitIdx])
		}
		if senderNickname == "" {
			senderNickname = senderUser
		}
	}

	if groupId != "" {
		userID2NicknameMap.Store(groupId+"_"+senderUser, senderNickname)
	}

	wechatMsg := &WechatMessage{
		GroupId:     groupId,
		SelfID:      selfId,
		UserID:      senderUser,
		Sender:      &Sender{UserID: senderUser, Nickname: senderNickname},
		Time:        time.Now().UnixMilli(),
		PostType:    "message",
		MessageId:   msgId,
		Message:     messages,
		MsgResource: xmlStr,
		RawMessage:  content,
		ShowContent: userContent,
		MessageType: msgType,
	}

	return json.Marshal(wechatMsg)
}

func getMessagesFromProto(content, sender string, mediaContent []byte) []*Message {
	var messages []*Message

	if strings.Contains(sender, "@chatroom") {
		splitIndex := strings.Index(content, ":")
		pureContent := ""
		if splitIndex >= 0 {
			pureContent = strings.TrimSpace(content[splitIndex+1:])
		} else {
			pureContent = content
		}

		parts := strings.Split(pureContent, "\u2005")
		for _, part := range parts {
			part = strings.TrimSpace(part)
			if part == "" {
				continue
			}
			messages = append(messages, classifyMessage(part, nil))
		}
	} else {
		messages = append(messages, classifyMessage(content, mediaContent))
	}

	return messages
}

func classifyMessage(content string, mediaContent []byte) *Message {
	content = strings.ReplaceAll(content, "\t", "")
	content = strings.ReplaceAll(content, "\n", "")
	switch {
	case strings.HasPrefix(content, "<?xml version=\"1.0\"?><msg><img"):
		return &Message{Type: "image", Data: &SendRequestData{Text: content}}
	case strings.HasPrefix(content, "<msg><voicemsg"):
		if mediaContent != nil {
			// 找到 silk 音频数据起始位置
			for i, b := range mediaContent {
				if b == 0x02 {
					mediaContent = mediaContent[i:]
					break
				}
			}
			return &Message{Type: "record", Data: &SendRequestData{Text: content, Media: mediaContent}}
		}
		return &Message{Type: "record", Data: &SendRequestData{Text: content}}
	case strings.HasPrefix(content, "<?xml version=\"1.0\"?><msg><appmsg"):
		re := regexp.MustCompile(`<type>(.*?)</type>`)
		match := re.FindStringSubmatch(content)
		if len(match) > 1 {
			switch match[1] {
			case "5":
				return &Message{Type: "share", Data: &SendRequestData{Text: content}}
			case "6":
				return &Message{Type: "file", Data: &SendRequestData{Text: content}}
			}
		}
		return &Message{Type: "text", Data: &SendRequestData{Text: content}}
	case strings.HasPrefix(content, "<msg><emoji"):
		return &Message{Type: "face", Data: &SendRequestData{Text: content}}
	case strings.HasPrefix(content, "<?xml version=\"1.0\"?><msg><videomsg"):
		return &Message{Type: "video", Data: &SendRequestData{Text: content}}
	case strings.HasPrefix(content, "<sysmsg") || strings.HasPrefix(content, "<?xml version=\"1.0\"?><sysmsg") || strings.HasPrefix(content, "<msg><op id"):
		return &Message{Type: "sys", Data: &SendRequestData{Text: content}}
	default:
		return &Message{Type: "text", Data: &SendRequestData{Text: content}}
	}
}
