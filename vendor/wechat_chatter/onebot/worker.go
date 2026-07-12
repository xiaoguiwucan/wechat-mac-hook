package main

import (
	"context"
	"crypto/aes"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"os"
	"runtime/debug"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

func SendWorker() {
	defer func() {
		if err := recover(); err != nil {
			Error("SendWorker panic", "err", err, "stack", string(debug.Stack()))
			go SendWorker()
		}
	}()

	for {
		select {
		case m, ok := <-msgChan:
			if !ok {
				Fatal("发送通道关闭")
				return
			}
			SendWechatMsg(m)
		}
	}
}

func SendWechatMsg(m *SendMsg) {
	var sendErr error
	defer func() {
		if m.ResultChan != nil {
			m.ResultChan <- sendErr
		}
	}()

	time.Sleep(time.Duration(config.SendInterval) * time.Millisecond)
	currTaskId := atomic.AddInt64(&taskId, 1)
	Info("📩 收到任务", "task_id", currTaskId, "type", m.Type)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	targetId := m.UserId
	if m.GroupID != "" {
		targetId = m.GroupID
	}

	if targetId == "" {
		Error("目标为空", "task_id", currTaskId, "target_id", targetId)
		sendErr = errors.New("target is empty")
		return
	}

	switch m.Type {
	case "text":
		protoHex, err := BuildTextMsgProto(targetId, m.Content, m.AtUser)
		if err != nil {
			Error("构建文本protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "text")
		result := fridaScript.ExportsCall("triggerSendTextMessage", currTaskId, targetId, m.Content, m.AtUser, protoHex, payloadHex)
		Info("📩 发送文本任务执行结果", "result", result, "task_id", currTaskId, "target_id", targetId, "at_user", m.AtUser)
		if result != "1" {
			Error("发送文本失败", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send text failed")
			return
		}
	case "image":
		targetPath, md5Str, err := SaveBase64Image(m.Content)
		if err != nil {
			Error("保存图片失败", "err", err)
			sendErr = err
			return
		}

		uploadPayloadHex := BuildUploadPayload("img")
		result := fridaScript.ExportsCall("triggerUploadImg", targetId, md5Str, targetPath, uploadPayloadHex)
		Info("📩 上传图片任务执行结果", "result", result, "target_id", targetId, "md5", md5Str, "path", targetPath)
		if result != "0" {
			Error("上传图片失败", "target_id", targetId, "md5", md5Str, "result", result)
			sendErr = errors.New("upload image failed")
			return
		}
		if m.ResultChan != nil {
			pendingResultMap.Store(targetId, m.ResultChan)
			m.ResultChan = nil // 不让 defer 发送结果
		}
		return
	case "send_image":
		protoHex, err := BuildImgMsgProto(myWechatId, targetId, m.CdnKey, m.AesKey, m.Md5Key)
		if err != nil {
			Error("构建图片protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "img")
		result := fridaScript.ExportsCall("triggerSendImgMessage", currTaskId, myWechatId, targetId, protoHex, payloadHex)
		Info("📩 发送图片任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
		if result != "1" {
			Error("发送图片失败", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send image failed")
			return
		}
	case "video":
		targetPath, md5Str, err := SaveBase64Image(m.Content)
		if err != nil {
			Error("保存图片失败", "err", err)
			sendErr = err
			return
		}

		// 获取视频时长和文件大小
		info := &VideoInfo{}
		duration, err := GetVideoDuration(targetPath)
		if err != nil {
			Error("获取视频时长失败", "err", err)
		} else {
			info.Duration = duration
		}
		if fi, err := os.Stat(targetPath); err == nil {
			info.VideoSize = int32(fi.Size())
		}
		videoInfoMap.Store(targetId, info)

		uploadPayloadHex := BuildUploadPayload("video")
		result := fridaScript.ExportsCall("triggerUploadVideo", targetId, md5Str, targetPath, uploadPayloadHex)
		Info("📩 上传视频任务执行结果", "result", result, "target_id", targetId, "md5", md5Str, "path", targetPath, "duration", info.Duration, "size", info.VideoSize)
		if result != "0" {
			Error("上传视频失败", "target_id", targetId, "md5", md5Str, "result", result)
			sendErr = errors.New("upload video failed")
			return
		}
		if m.ResultChan != nil {
			pendingResultMap.Store(targetId, m.ResultChan)
			m.ResultChan = nil
		}
		return
	case "send_video":
		var duration, videoSize int32
		if info, ok := videoInfoMap.LoadAndDelete(targetId); ok {
			vi := info.(*VideoInfo)
			duration = vi.Duration
			videoSize = vi.VideoSize
		}
		protoHex, err := BuildVideoMsgProto(myWechatId, targetId, m.CdnKey, m.AesKey, m.Md5Key, m.VideoId, duration, videoSize)
		if err != nil {
			Error("构建视频protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "video")
		result := fridaScript.ExportsCall("triggerSendVideoMessage", currTaskId, myWechatId, targetId, protoHex, payloadHex)
		Info("📩 发送视频任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId, "duration", duration, "size", videoSize)
		if result != "1" {
			Error("发送视频失败", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send video failed")
			return
		}
	case "download":
		result := fridaScript.ExportsCall("triggerDownload", targetId, m.FIleCdnUrl, m.AesKey, m.FilePath, m.FileType)
		Info("📩 下载任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
	case "reply":
		replyInfo := &ReplyInfo{
			Content:     m.Content,
			MsgId:       m.ReferMsgId,
			MsgSender:   m.ReferMsgSender,
			MsgType:     m.ReferMsgType,
			CreateTime:  m.ReferCreateTime,
			Msgsource:   m.ReferMsgsource,
			DisplayName: m.ReferDisplayName,
			MsgContent:  m.ReferContent,
		}
		protoHex, err := BuildReplyMsgProto(myWechatId, targetId, replyInfo)
		if err != nil {
			Error("构建回复protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "reply")
		result := fridaScript.ExportsCall("triggerSendReplyMessage", currTaskId, myWechatId, targetId, protoHex, payloadHex)
		Info("📩 发送回复任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
		if result != "1" {
			Error("发送回复失败", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send reply failed")
			return
		}
	case "voice":
		// 直接base64解码，不追加salt（音频二进制不能被修改）
		rawAudio, targetPath, err := SaveVoiceFile(m.Content)
		if err != nil {
			Error("保存语音文件失败", "err", err)
			sendErr = err
			return
		}

		// 转换为SILK格式
		silkData, voiceDurationMs, err := ConvertToSilk(rawAudio)
		if err != nil {
			Error("转换SILK格式失败", "err", err)
			sendErr = err
			return
		}

		audioHex := hex.EncodeToString(silkData)

		uploadPayloadHex := BuildVoiceUploadPayload()
		result := fridaScript.ExportsCall("triggerUploadVoice", targetId, targetPath, uploadPayloadHex, audioHex, voiceDurationMs)
		Info("📩 上传语音任务执行结果", "result", result, "target_id", targetId, "path", targetPath, "silk_len", len(silkData), "duration_ms", voiceDurationMs)
		if result != "0" {
			Error("上传语音失败", "target_id", targetId, "result", result)
			sendErr = errors.New("upload voice failed")
			return
		}
		if m.ResultChan != nil {
			pendingResultMap.Store(targetId, m.ResultChan)
			m.ResultChan = nil
		}
		return
	case "send_voice":
		protoHex, err := BuildVoiceMsgProto(myWechatId, targetId, m.CdnKey, m.AesKey, m.VoiceDuration, m.SilkDataLen, m.Unknown13)
		if err != nil {
			Error("构建语音protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "voice")
		result := fridaScript.ExportsCall("triggerSendVoiceMessage", currTaskId, myWechatId, targetId, protoHex, payloadHex)
		Info("📩 发送语音任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId, "unknown13", m.Unknown13)
		if result != "1" {
			Error("发送语音失败", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send voice failed")
			return
		}
	case "send_file_simple":
		// iPad860 风格: uploadappattach 分片直传 → sendappmsg，不走 CDN。
		// 文件名/扩展名由内容自动识别 + 时间戳随机生成(SaveBase64File 内部完成)。
		targetPath, _, err := SaveBase64File(m.Content, "")
		if err != nil {
			Error("保存文件失败", "err", err)
			sendErr = err
			return
		}

		chunks, fileInfo, err := BuildUploadAppAttachChunks(targetId, targetPath)
		if err != nil {
			Error("构建uploadappattach分片失败", "err", err)
			sendErr = err
			return
		}
		Info("📩 开始uploadappattach直传", "target_id", targetId, "chunks", len(chunks),
			"file_name", fileInfo.FileName, "file_ext", fileInfo.FileExt,
			"total_len", fileInfo.TotalLen, "md5", fileInfo.Md5)

		var attachId string
		for i, chunkHex := range chunks {
			chunkTaskId := atomic.AddInt64(&taskId, 1)
			payloadHex := BuildSendPayload(chunkTaskId, "appattach")
			result := fridaScript.ExportsCall("triggerUploadAppAttach", chunkTaskId, myWechatId, targetId, chunkHex, payloadHex)
			if result != "1" {
				Error("uploadappattach分片发送失败", "chunk", i, "result", result)
				sendErr = errors.New("upload app attach chunk failed")
				return
			}

			select {
			case <-ctx.Done():
				Error("等待uploadappattach响应超时", "chunk", i)
				sendErr = errors.New("upload app attach timeout")
				return
			case data := <-appAttachRespChan:
				id, perr := ParseUploadAppAttachResponse(data)
				if perr != nil {
					Error("解析uploadappattach响应失败", "chunk", i, "err", perr)
					sendErr = perr
					return
				}
				if id != "" {
					attachId = id
				}
				Info("📩 uploadappattach分片完成", "chunk", i, "attach_id", id)
			}
		}

		if attachId == "" {
			Error("uploadappattach未返回attachId", "target_id", targetId)
			sendErr = errors.New("upload app attach no attachId")
			return
		}
		fileInfo.AttachId = attachId

		// sendappmsg (type=6)，精简版 appmsg，cdnattachurl 也填 attachId
		currTaskId = atomic.AddInt64(&taskId, 1)
		protoHex, err := BuildSimpleFileMsgProto(myWechatId, targetId, fileInfo)
		if err != nil {
			Error("构建文件protobuf失败", "err", err)
			sendErr = err
			return
		}
		payloadHex := BuildSendPayload(currTaskId, "file")
		result := fridaScript.ExportsCall("triggerSendFileMessage", currTaskId, myWechatId, targetId, protoHex, payloadHex)
		Info("📩 发送文件消息(simple)执行结果", "result", result, "task_id", currTaskId, "target_id", targetId)
		if result != "1" {
			Error("发送文件失败(simple)", "task_id", currTaskId, "target_id", targetId, "result", result)
			sendErr = errors.New("send file failed")
			return
		}
	}

	select {
	case <-ctx.Done():
		Error("任务执行超时！", "taskId", currTaskId)
		sendErr = errors.New("send timeout")
	case resp := <-buf2RespChan:
		if resp.Err != nil {
			Error("收到buf2resp失败信号", "taskId", currTaskId, "msg_type", resp.MsgType, "err", resp.Err)
			sendErr = resp.Err
			return
		}
		Info("收到buf2resp完成信号，任务完成", "taskId", currTaskId, "msg_type", resp.MsgType, "data_len", len(resp.Data))
	}
}

func HandleMsg(jsonData []byte) ([]byte, error) {
	m := new(WechatMessage)
	err := json.Unmarshal(jsonData, m)
	if err != nil {
		Error("解析消息失败", "err", err)
		return nil, err
	}

	if myWechatId == "" && m.SelfID != "" {
		myWechatId = m.SelfID
	}
	if m.GroupId != "" {
		userID2NicknameMap.Store(m.GroupId+"_"+m.UserID, m.Sender.Nickname)
	}

	for _, msg := range m.Message {
		switch msg.Type {
		case "record":
			path, err := SaveAudioFile(msg.Data.Media)
			if err != nil {
				Error("保存音频失败", "err", err)
				return nil, err
			}
			msg.Data.URL = "file://" + path
			msg.Data.Media = nil
		case "image":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				return nil, err
			}

			path, err := GetDownloadPath(fileMsg.Image.MidImgURL, fileMsg.Image.AesKey, "", 0)
			if err != nil {
				Error("获取文件路径失败", "err", err)
				return nil, err
			}

			msg.Data.URL = "file://" + path

		case "file":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				return nil, err
			}
			totalLen, _ := strconv.Atoi(strings.TrimSpace(fileMsg.AppMsg.AppAttach.TotalLen))
			path, err := GetDownloadPath(fileMsg.AppMsg.AppAttach.CdnAttachURL, fileMsg.AppMsg.AppAttach.AesKey, fileMsg.AppMsg.AppAttach.FileExt, totalLen)
			if err != nil {
				Error("获取文件路径失败", "err", err)
				return nil, err
			}

			msg.Data.URL = "file://" + path
		case "video":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				return nil, err
			}
			path, err := GetDownloadPath(fileMsg.Video.CdnVideoUrl, fileMsg.Video.AesKey, "mp4", int(fileMsg.Video.Length))
			if err != nil {
				Error("获取文件路径失败", "err", err)
				return nil, err
			}

			msg.Data.URL = "file://" + path
		case "face":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				return nil, err
			}

			// 优先cdnurl，为空则用thumburl，再为空则用externurl
			emojiUrl := fileMsg.Emoji.CdnUrl
			if emojiUrl == "" {
				emojiUrl = fileMsg.Emoji.ThumbUrl
			}
			if emojiUrl == "" {
				emojiUrl = fileMsg.Emoji.ExternUrl
			}

			data, err := DownloadFile(emojiUrl)
			if err != nil {
				Error("下载表情失败", "err", err)
				return nil, err
			}

			path, err := DetectAndSaveImage(data)
			if err != nil {
				Error("保存表情失败", "err", err)
				return nil, err
			}

			msg.Data.URL = "file://" + path
		}
	}
	return json.Marshal(m)
}

func GetDownloadPath(cdnUrl, aesKeyStr, extHint string, totalLen int) (string, error) {
	for i := 0; i < 30; i++ {
		if downloadMsgInter, ok := userID2FileMsgMap.Load(cdnUrl); ok {
			downloadReq := downloadMsgInter.(*DownloadRequest)

			downloadReq.mu.Lock()

			if downloadReq.FilePath != "" {
				fp := downloadReq.FilePath
				downloadReq.mu.Unlock()
				return fp, nil
			}

			// 检查数据是否还在接收中
			timeSinceLastAppend := time.Now().UnixMilli() - downloadReq.LastAppendTime
			Info("文件等待下载", "url", cdnUrl, "times", i, "last_append_time", timeSinceLastAppend)

			// 如果数据仍在接收中（3秒内有新数据），继续等待
			if timeSinceLastAppend < 2000 && i < 29 {
				downloadReq.mu.Unlock()
				time.Sleep(2 * time.Second)
				continue
			}

			// 数据接收完成，尝试解密
			if len(downloadReq.Media) > 0 {
				media := downloadReq.Media
				// AES 块对不齐时，末尾补 0 到整块（微信 CDN 密文尾部可能带
				// 非整块残余，补齐后按整块解密，避免丢字节）。
				if rem := len(media) % aes.BlockSize; rem != 0 {
					pad := aes.BlockSize - rem
					Info("文件数据未对齐 AES 块，末尾补 0 到整块",
						"url", cdnUrl, "media_len", len(media), "pad", pad, "block_size", aes.BlockSize)
					padded := make([]byte, len(media)+pad)
					copy(padded, media)
					media = padded
				}

				aesKey, err := hex.DecodeString(aesKeyStr)
				if err != nil {
					downloadReq.mu.Unlock()
					Error("AES key 解码失败", "err", err)
					return "", err
				}
				filePath, err := GetFilePath(media, aesKey, extHint, totalLen)
				if err != nil {
					downloadReq.mu.Unlock()
					Error("获取文件路径失败", "err", err, "media_len", len(media), "aes_key", aesKeyStr)
					userID2FileMsgMap.Delete(cdnUrl)
					return "", err
				}

				downloadReq.FilePath = filePath
				downloadReq.Media = nil
				downloadReq.mu.Unlock()
				return filePath, nil
			}

			downloadReq.mu.Unlock()
		}

		time.Sleep(2 * time.Second)
	}

	return "", errors.New("文件下载超时或数据为空")
}

// HandleBuf2Resp 处理所有消息类型的buf2resp响应
func HandleBuf2Resp(msgType string, data []byte) {
	Info("收到buf2resp响应", "msg_type", msgType, "data_len", len(data))

	if len(data) == 0 {
		Error("buf2resp响应数据为空", "msg_type", msgType)
		buf2RespChan <- &Buf2RespData{
			MsgType: msgType,
			Data:    data,
			Err:     errors.New("response data is empty"),
		}
		return
	}

	switch msgType {
	case "appattach":
		Info("收到uploadappattach响应", "data_len", len(data))
		appAttachRespChan <- data
		return
	}

	ret, errMsg, err := ParseSendMsgResponse(data)
	if err != nil {
		Info("buf2resp响应无法提取错误码，视为成功", "msg_type", msgType, "err", err)
	}

	// 判断错误码是否为0
	if ret != 0 {
		Error("buf2resp响应错误", "msg_type", msgType, "ret", ret, "errMsg", errMsg)
		buf2RespChan <- &Buf2RespData{
			MsgType: msgType,
			Data:    data,
			Err:     fmt.Errorf("response error, ret=%d, errMsg=%s", ret, errMsg),
		}
		return
	}

	Info("buf2resp响应成功", "msg_type", msgType)
	buf2RespChan <- &Buf2RespData{
		MsgType: msgType,
		Data:    data,
	}
}
