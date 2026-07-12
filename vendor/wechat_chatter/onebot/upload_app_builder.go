package main

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"os"
	"time"

	"google.golang.org/protobuf/proto"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
)

// uploadAppAttachChunkSize 每个分片大小 (参考 we-chat-ipad860 的 50000 字节)
const uploadAppAttachChunkSize = 50000

// BuildUploadAppAttachChunks 读取文件并按分片构造 uploadappattach 请求的 protobuf hex 列表。
// 返回: 每个分片的 protobuf hex、填充好基础信息的 FileInfo、错误。
// 参考 we-chat-ipad860 models/Tools/UploadApp.go 的 SendUploadAppAttach。
func BuildUploadAppAttachChunks(receiver, filePath string) ([]string, *FileInfo, error) {
	fileData, err := os.ReadFile(filePath)
	if err != nil {
		return nil, nil, fmt.Errorf("读取文件失败: %w", err)
	}

	total := len(fileData)
	if total == 0 {
		return nil, nil, fmt.Errorf("文件为空: %s", filePath)
	}

	sum := md5.Sum(fileData)
	fileMd5 := hex.EncodeToString(sum[:])

	// clientAppDataId: {receiver}_{ts}_UploadFile (对齐 iPad860)
	clientAppDataId := fmt.Sprintf("%s_%d_UploadFile", receiver, time.Now().Unix())

	fileInfo := &FileInfo{
		FileName:    extractFileName(filePath),
		TotalLen:    int64(total),
		FileExt:     getFileExt(filePath),
		Md5:         fileMd5,
		ClientMsgId: clientAppDataId,
	}

	var hexChunks []string
	for startPos := 0; startPos < total; startPos += uploadAppAttachChunkSize {
		end := startPos + uploadAppAttachChunkSize
		if end > total {
			end = total
		}
		chunk := fileData[startPos:end]

		hexStr, err := buildUploadAppAttachChunk(receiver, clientAppDataId, fileMd5,
			uint32(total), uint32(startPos), chunk)
		if err != nil {
			return nil, nil, err
		}
		hexChunks = append(hexChunks, hexStr)
	}

	return hexChunks, fileInfo, nil
}

// buildUploadAppAttachChunk 构造单个分片的 UploadAppAttachRequest protobuf hex。
func buildUploadAppAttachChunk(receiver, clientAppDataId, fileMd5 string,
	totalLen, startPos uint32, chunk []byte) (string, error) {

	version := NextVersion()
	appId := ""
	sdkVersion := uint32(0)
	dataLen := uint32(len(chunk))
	fileType := uint32(6) // 6 = 文件

	req := &wxproto.UploadAppAttachRequest{
		BaseRequest: &wxproto.ReplyMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   &globalSessionId,
			ClientProof: globalClientProof,
			DeviceId:    &globalDeviceId,
			Platform:    proto.String("UnifiedPCMac 26 arm64"),
			Version:     &version,
		},
		AppId:           &appId,
		SdkVersion:      &sdkVersion,
		ClientAppDataId: &clientAppDataId,
		UserName:        &receiver,
		TotalLen:        &totalLen,
		StartPos:        &startPos,
		DataLen:         &dataLen,
		Data: &wxproto.SKBuiltinBufferT{
			ILen:   &dataLen,
			Buffer: chunk,
		},
		Type: &fileType,
		Md5:  &fileMd5,
	}

	data, err := proto.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal uploadappattach chunk failed: %w", err)
	}

	//fmt.Printf("[uploadappattach-proto] startPos=%d dataLen=%d totalLen=%d len=%d\n%s\n",
	//	startPos, dataLen, totalLen, len(data), HexDump(data, 0))

	return hex.EncodeToString(data), nil
}

// ParseUploadAppAttachResponse 解析 uploadappattach 响应，提取 mediaId(attachid)。
func ParseUploadAppAttachResponse(data []byte) (string, error) {
	// fmt.Printf("[uploadappattach-resp] len=%d\n%s\n", len(data), HexDump(data, 0))

	resp := &wxproto.UploadAppAttachResponse{}
	if err := proto.Unmarshal(data, resp); err != nil {
		return "", fmt.Errorf("unmarshal uploadappattach response failed: %w", err)
	}

	if resp.BaseResponse != nil && resp.BaseResponse.Ret != nil && *resp.BaseResponse.Ret != 0 {
		errMsg := ""
		if resp.BaseResponse.ErrMsg != nil {
			errMsg = resp.BaseResponse.ErrMsg.GetMsg()
		}
		return "", fmt.Errorf("uploadappattach failed, ret=%d, errMsg=%s",
			*resp.BaseResponse.Ret, errMsg)
	}

	return resp.GetMediaId(), nil
}

// BuildSimpleFileMsgProto 构建发送文件消息的 protobuf（uploadappattach 直传后用）。
// 严格按 wechat7016 WXSendMsgFile / SendAppMsgRequest 格式：
//   - 顶层 SendAppMsgReq{baseRequest, msg=AppMsgBody{...}}
//   - AppMsgBody 只填 fromUserName/toUserName/type=6/content(XML)/clientMsgId/createTime
//   - appmsg XML 极简：只有 title/type/appattach(totallen/attachid/fileext)，不含 cdnattachurl
func BuildSimpleFileMsgProto(sender, receiver string, fileInfo *FileInfo) (string, error) {
	now := time.Now().Unix()

	// wechat7016 WXSendMsgFile 的极简 XML（attachid 用完整 mediaId，不加 cdnattachurl）
	xml := `<?xml version="1.0"?>` + "\n"
	xml += `<appmsg appid='' sdkver=''>`
	xml += `<title>` + escapeXmlStr(fileInfo.FileName) + `</title>`
	xml += `<des></des>`
	xml += `<action></action>`
	xml += `<type>6</type>`
	xml += `<content></content>`
	xml += `<url></url>`
	xml += `<lowurl></lowurl>`
	xml += `<appattach>`
	xml += `<totallen>` + fmt.Sprintf("%d", fileInfo.TotalLen) + `</totallen>`
	xml += `<attachid>` + escapeXmlStr(fileInfo.AttachId) + `</attachid>`
	xml += `<fileext>` + escapeXmlStr(fileInfo.FileExt) + `</fileext>`
	xml += `</appattach>`
	xml += `<extinfo></extinfo>`
	xml += `</appmsg>`

	version := NextVersion()
	appId := ""
	sdkVersion := uint32(0)
	msgType := uint32(6) // 6 = 文件
	clientMsgId := fmt.Sprintf("%d", now)
	msgSource := "<msgsource><alnode><fr>1</fr><cf>2</cf></alnode></msgsource>"

	req := &wxproto.SendAppMsgReq{
		BaseRequest: &wxproto.ReplyMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   &globalSessionId,
			ClientProof: globalClientProof,
			DeviceId:    &globalDeviceId,
			Platform:    proto.String("UnifiedPCMac 26 arm64"),
			Version:     &version,
		},
		Msg: &wxproto.AppMsgBody{
			FromUserName: &sender,
			AppId:        &appId,
			SdkVersion:   &sdkVersion,
			ToUserName:   &receiver,
			Type:         &msgType,
			Content:      &xml,
			CreateTime:   proto.Int64(now),
			ClientMsgId:  &clientMsgId,
			MsgSource:    &msgSource,
		},
	}

	data, err := proto.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal SendAppMsgReq failed: %w", err)
	}

	// fmt.Printf("[simple-file-proto] xml=%s\n[simple-file-proto] hex dump:\n%s\n", xml, HexDump(data, 0))

	return hex.EncodeToString(data), nil
}
