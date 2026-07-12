package main

import (
	"encoding/hex"
	"fmt"
	"math/rand"
	"time"

	"google.golang.org/protobuf/proto"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
)

// BuildFileUploadMsgProto 构建 /cgi-bin/micromsg-bin/sendfileuploadmsg 请求的protobuf
func BuildFileUploadMsgProto(targetId string, fileInfo *FileInfo) (string, error) {
	// 复用上传时生成的 clientMsgId（去掉 _1 后缀），保证与 upload body 里的 filekey 一致
	clientMsgId := fileInfo.ClientMsgId
	if clientMsgId == "" {
		now := time.Now().Unix()
		clientMsgId = fmt.Sprintf("%s_%d_%d", targetId, now, rand.Intn(1000))
		fileInfo.ClientMsgId = clientMsgId
	}

	version := NextVersion()
	msgType := uint32(1)
	field7 := uint32(0)
	field12 := uint32(0)
	fileSize := uint32(fileInfo.TotalLen)

	msg := &wxproto.SendFileUploadMsgRequest{
		BaseRequest: &wxproto.ReplyMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   &globalSessionId,
			ClientProof: globalClientProof,
			DeviceId:    &globalDeviceId,
			Platform:    proto.String("UnifiedPCMac 26 arm64"),
			Version:     &version,
		},
		ToUserName:  &targetId,
		ClientMsgId: &clientMsgId,
		MsgType:     &msgType,
		FileInfo: &wxproto.FileUploadFileInfo{
			FileName: &fileInfo.FileName,
			FileExt:  &fileInfo.FileExt,
			FileMd5:  &fileInfo.Md5,
			FileSize: &fileSize,
		},
		Field7:  &field7,
		Field12: &field12,
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal file upload msg proto failed: %w", err)
	}

	fmt.Printf("[file-upload-proto] final protobuf hex dump:\n%s\n", HexDump(data, 0))
	return hex.EncodeToString(data), nil
}

// ParseFileUploadMsgResponse 解析 sendfileuploadmsg 响应，提取 fileUploadToken 和 msgSvrId
func ParseFileUploadMsgResponse(data []byte) (string, string, error) {
	resp := &wxproto.SendFileUploadMsgResponse{}
	err := proto.Unmarshal(data, resp)
	if err != nil {
		return "", "", fmt.Errorf("unmarshal file upload response failed: %w", err)
	}

	if resp.BaseResponse != nil && resp.BaseResponse.Ret != nil && *resp.BaseResponse.Ret != 0 {
		errMsg := ""
		if resp.BaseResponse.ErrMsg != nil {
			errMsg = resp.BaseResponse.ErrMsg.GetMsg()
		}
		return "", "", fmt.Errorf("sendfileuploadmsg failed, ret=%d, errMsg=%s", *resp.BaseResponse.Ret, errMsg)
	}

	token := resp.GetFileUploadToken()
	msgSvrId := fmt.Sprintf("%d", resp.GetMsgSvrId())
	return token, msgSvrId, nil
}

// FileInfo 文件发送消息需要的信息
type FileInfo struct {
	FileName        string // 文件名 (title)
	TotalLen        int64  // 文件大小
	AttachId        string // 附件ID (来自upload完成回调)
	FileExt         string // 文件扩展名
	CdnAttachURL    string // CDN附件URL
	AesKey          string // AES密钥
	Md5             string // 文件MD5
	OverwriteMsgId  string // overwrite_newmsgid
	FileUploadToken string // fileuploadtoken
	ClientMsgId     string // 由 BuildFileUploadMsgProto 生成，BuildFileMsgProto 复用
}

// BuildFileMsgProto 构建发送文件消息的protobuf并返回hex编码的字符串
// 复用reply_msg.proto (WxSendReplyMsg)，msg_type=6
func BuildFileMsgProto(sender, receiver string, fileInfo *FileInfo) (string, error) {
	now := time.Now().Unix()

	// 构建文件appmsg XML
	appmsgXml := buildFileAppmsgXml(sender, fileInfo)

	// 构建客户端消息ID: 复用 BuildFileUploadMsgProto 的 clientMsgId + 后缀
	clientMsgId := fileInfo.ClientMsgId + "_xwechat_1"

	// msgsource
	msgsource := "<msgsource><alnode><fr>1</fr><cf>2</cf></alnode></msgsource>"

	// proto2 需要使用指针
	var (
		unknown2  = []byte("wx6618f1cfc6c132f8") // appid
		unknown3  = int32(0)
		msgType   = int32(6) // 文件消息类型
		flag      = int32(1)
		unknown13 = []byte{}
		unknown14 = []byte{}
		unknown15 = []byte{}
		version   = NextVersion()
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
		Unknown5:  []byte(fileInfo.Md5),
		Unknown9:  proto.Int32(1),
		Unknown10: proto.Uint64(uint64(rand.Uint32())),
		Unknown11: proto.Int32(2),
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal file proto failed: %w", err)
	}

	fmt.Printf("[file-proto] final protobuf hex dump:\n%s\n", HexDump(data, 0))

	return hex.EncodeToString(data), nil
}

// buildFileAppmsgXml 构建文件消息的appmsg XML
func buildFileAppmsgXml(selfWxid string, info *FileInfo) string {
	xml := `<appmsg appid="wx6618f1cfc6c132f8" sdkver="0">`
	xml += `<title>` + escapeXmlStr(info.FileName) + `</title>`
	xml += `<des></des>`
	xml += `<action></action>`
	xml += `<type>6</type>`
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
	xml += `<appattach>`
	xml += `<totallen>` + fmt.Sprintf("%d", info.TotalLen) + `</totallen>`
	xml += `<attachid>` + escapeXmlStr(info.AttachId) + `</attachid>`
	xml += `<emoticonmd5></emoticonmd5>`
	xml += `<fileext>` + escapeXmlStr(info.FileExt) + `</fileext>`
	xml += `<cdnattachurl>` + escapeXmlStr(info.CdnAttachURL) + `</cdnattachurl>`
	xml += `<aeskey>` + escapeXmlStr(info.AesKey) + `</aeskey>`
	xml += `<encryver>0</encryver>`
	xml += `<overwrite_newmsgid>` + escapeXmlStr(info.OverwriteMsgId) + `</overwrite_newmsgid>`
	xml += `<fileuploadtoken>` + escapeXmlStr(info.FileUploadToken) + `</fileuploadtoken>`
	xml += `</appattach>`
	xml += `<extinfo></extinfo>`
	xml += `<sourceusername></sourceusername>`
	xml += `<sourcedisplayname></sourcedisplayname>`
	xml += `<thumburl></thumburl>`
	xml += `<md5>` + escapeXmlStr(info.Md5) + `</md5>`
	xml += `<statextstr></statextstr>`
	xml += `</appmsg>`
	xml += `<fromusername>` + escapeXmlStr(selfWxid) + `</fromusername>`

	return xml
}

// BuildCheckMd5Proto 构建 /cgi-bin/micromsg-bin/checkmd5 请求的protobuf
func BuildCheckMd5Proto(fileInfo *FileInfo) (string, error) {
	version := NextVersion()
	field5 := uint32(0)

	msg := &wxproto.CheckMd5Request{
		BaseRequest: &wxproto.ReplyMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   &globalSessionId,
			ClientProof: globalClientProof,
			DeviceId:    &globalDeviceId,
			Platform:    proto.String("UnifiedPCMac 26 arm64"),
			Version:     &version,
		},
		FileKey: &fileInfo.CdnAttachURL,
		FileMd5: &fileInfo.Md5,
		Field4:  []byte{},
		Field5:  &field5,
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal checkmd5 proto failed: %w", err)
	}

	//fmt.Printf("[checkmd5-proto] final protobuf hex dump:\n%s\n", HexDump(data, 0))
	return hex.EncodeToString(data), nil
}

// ParseCheckMd5Response 解析 checkmd5 响应
// ret=0 表示文件已存在(秒传)，ret=102 表示文件不存在需要正常发送，两者都视为成功
func ParseCheckMd5Response(data []byte) error {
	resp := &wxproto.CheckMd5Response{}
	err := proto.Unmarshal(data, resp)
	if err != nil {
		return fmt.Errorf("unmarshal checkmd5 response failed: %w", err)
	}

	if resp.BaseResponse != nil && resp.BaseResponse.Ret != nil {
		ret := *resp.BaseResponse.Ret
		// 0=文件已存在(秒传), 102=文件不存在(正常发送), 都可以继续
		if ret != 0 {
			errMsg := ""
			if resp.BaseResponse.ErrMsg != nil {
				errMsg = resp.BaseResponse.ErrMsg.GetMsg()
			}
			return fmt.Errorf("checkmd5 failed, ret=%d, errMsg=%s", ret, errMsg)
		}
		Info("checkmd5 ret", "ret", ret)
	}

	return nil
}
