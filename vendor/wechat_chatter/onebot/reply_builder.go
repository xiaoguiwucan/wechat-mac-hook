package main

import (
	"encoding/hex"
	"fmt"
	"math/rand"
	"time"

	"google.golang.org/protobuf/proto"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
)

// BuildReplyMsgProto 构建发送回复消息的protobuf并返回hex编码的字符串
func BuildReplyMsgProto(sender, receiver string, replyInfo *ReplyInfo) (string, error) {
	now := time.Now().Unix()

	// 构建appmsg XML (需要传入自己的wxid用于fromusername)
	appmsgXml := buildReplyAppmsgXml(sender, replyInfo)

	// 构建客户端消息ID (使用对方的wxid，即receiver)
	clientMsgId := fmt.Sprintf("%s_%d_%d_xwechat_3", receiver, now, rand.Intn(100))

	// msgsource
	msgsource := "<msgsource><alnode><fr>1</fr></alnode></msgsource>"

	// proto2 需要使用指针
	var (
		unknown2  = []byte{}
		unknown3  = int32(0)
		msgType   = int32(57)
		flag      = int32(1)
		unknown13 = []byte{}
		unknown14 = []byte{}
		unknown15 = []byte{}
		version   = uint32(161)
	)

	msg := &wxproto.WxSendReplyMsg{
		Header: &wxproto.ReplyMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   &globalSessionId,
			ClientProof: globalClientProof,
			DeviceId:    &globalDeviceId,
			Platform:    proto.String("UnifiedPCMac 26 arm64"),
			Version:     &version,
		},
		Body: &wxproto.ReplyMsgBody{
			Sender:        &sender,
			Unknown2:      unknown2,
			Unknown3:      &unknown3,
			Receiver:      &receiver,
			MsgType:       &msgType,
			Content:       []byte(appmsgXml),
			SendTimestamp: proto.Int64(now),
			ClientMsgId:   &clientMsgId,
			Flag:          &flag,
			Msgsource:     []byte(msgsource),
			Unknown13:     unknown13,
			Unknown14:     unknown14,
			Unknown15:     unknown15,
		},
		Unknown5:  []byte{},
		Unknown9:  proto.Int32(0),
		Unknown10: proto.Uint64(0),
		Unknown11: proto.Int32(2),
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal reply proto failed: %w", err)
	}

	//fmt.Println(fmt.Print(HexDump(data, 0x0000000C12334C00)))

	return hex.EncodeToString(data), nil
}

// ReplyInfo 回复消息的全部信息
type ReplyInfo struct {
	Content     string // 回复的文本内容
	MsgId       string // 被回复消息的svrid
	MsgSender   string // 被回复消息的发送者wxid
	MsgType     int    // 被回复消息的类型 (1=text, 3=image, 43=video, 49=appmsg)
	CreateTime  int64  // 被回复消息的时间戳(毫秒)
	Msgsource   string // 被回复消息的msgsource
	DisplayName string // 被回复消息发送者的昵称
	MsgContent  string // 被回复消息的内容
}

// buildReplyAppmsgXml 构建回复消息的appmsg XML，字段顺序匹配微信真实protobuf
func buildReplyAppmsgXml(selfWxid string, info *ReplyInfo) string {
	// 时间戳：毫秒转秒
	createTime := info.CreateTime / 1000

	xml := `<appmsg appid="" sdkver="0">`
	xml += `<title>` + escapeXmlStr(info.Content) + `</title>`
	xml += `<des></des>`
	xml += `<action></action>`
	xml += `<type>57</type>`
	xml += `<showtype>0</showtype>`
	xml += `<soundtype>0</soundtype>`
	xml += `<mediatagname></mediatagname>`
	xml += `<messageext></messageext>`
	xml += `<messageaction></messageaction>`
	xml += `<content></content>`
	xml += `<contentattr>0</contentattr>`
	xml += `<url></url>`
	xml += `<lowurl></lowurl>`
	xml += `<dataurl></dataurl>`
	xml += `<lowdataurl></lowdataurl>`
	xml += `<songalbumurl></songalbumurl>`
	xml += `<songlyric></songlyric>`
	xml += `<template_id></template_id>`
	xml += `<appattach><totallen>0</totallen><attachid></attachid><emoticonmd5></emoticonmd5><fileext></fileext><aeskey></aeskey></appattach>`
	xml += `<extinfo></extinfo>`
	xml += `<sourceusername></sourceusername>`
	xml += `<sourcedisplayname></sourcedisplayname>`
	xml += `<thumburl></thumburl>`
	xml += `<md5></md5>`
	xml += `<statextstr></statextstr>`

	// refermsg - 字段顺序与微信一致: chatusr → type → createtime → msgsource → displayname → svrid → fromusr → content
	xml += `<refermsg>`
	xml += `<chatusr>` + escapeXmlStr(info.MsgSender) + `</chatusr>`
	xml += `<type>` + fmt.Sprintf("%d", info.MsgType) + `</type>`
	xml += `<createtime>` + fmt.Sprintf("%d", createTime) + `</createtime>`
	xml += `<msgsource>` + escapeXmlStr(info.Msgsource) + `</msgsource>`
	xml += `<displayname></displayname>`
	xml += `<svrid>` + escapeXmlStr(info.MsgId) + `</svrid>`
	xml += `<fromusr>` + escapeXmlStr(info.MsgSender) + `</fromusr>`
	xml += `<content>` + escapeXmlStr(info.MsgContent) + `</content>`
	xml += `</refermsg>`
	xml += `</appmsg>`

	xml += `<fromusername>` + escapeXmlStr(selfWxid) + `</fromusername>`

	return xml
}

// escapeXmlStr 简单的XML转义
func escapeXmlStr(s string) string {
	result := ""
	for _, c := range s {
		switch c {
		case '&':
			result += "&amp;"
		case '<':
			result += "&lt;"
		case '>':
			result += "&gt;"
		case '"':
			result += "&quot;"
		case '\'':
			result += "&apos;"
		default:
			result += string(c)
		}
	}
	return result
}

// generateClientProof 生成n字节可见字符（模拟微信的client_proof格式）
func generateClientProof(n int) []byte {
	const chars = "0123456789abcdefghijklmnopqrstuvwxyz"
	b := make([]byte, n)
	for i := range b {
		b[i] = chars[rand.Intn(len(chars))]
	}
	return b
}
