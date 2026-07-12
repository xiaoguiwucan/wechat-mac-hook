package main

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"time"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/proto"
)

// BuildVideoMsgProto 构建发送视频消息的protobuf并返回hex编码的字符串
func BuildVideoMsgProto(sender, targetId, cdnKey, aesKey, md5Key, videoId string, duration int32, videoSize int32) (string, error) {
	// client_msg_id: targetId_timestamp_160_xwechat_1
	timestamp := time.Now().Unix()
	clientMsgId := targetId + "_" + strconv.FormatInt(timestamp, 10) + "_160_xwechat_1"

	// xml内容 (视频多了<cf>3</cf>)
	xmlContent := []byte("<msgsource><alnode><fr>1</fr><cf>3</cf></alnode></msgsource>")

	msg := &wxproto.WxSendVideoMsg{
		Header: &wxproto.VideoMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   int64(globalSessionId),
			ClientProof: globalClientProof,
			DeviceId:    int64(globalDeviceId),
			Platform:    []byte("UnifiedPCMac 26 arm64"),
			Version:     118,
		},
		ClientMsgId: []byte(clientMsgId),
		Sender:      []byte(sender),
		Receiver:    []byte(targetId),
		Unknown5:    14764,
		Unknown6:    14764,
		Unknown7: &wxproto.VideoMsgExtra{
			Field1: 0,
			Field2: []byte{},
		},
		VideoSize:  videoSize,
		VideoSize2: videoSize,
		Unknown10: &wxproto.VideoMsgExtra{
			Field1: 0,
			Field2: []byte{},
		},
		Duration:  duration,
		Unknown12: 1,
		Unknown13: 2,
		// Unknown14: 0, // 需要手动追加
		Xml:       xmlContent,
		CdnKey:    []byte(cdnKey),
		AesKey:    []byte(aesKey),
		Unknown18: 1,
		CdnKey2:   []byte(cdnKey),
		Unknown20: 14764,
		Unknown21: 360,
		Unknown22: 203,
		AesKey2:   []byte(aesKey),
		Md5Key:    []byte(md5Key),
		VideoId:   []byte(videoId),
		// Unknown38: 0, // 需要手动追加
		Md5Key2:    []byte(md5Key),
		CdnKey3:    []byte(cdnKey),
		AesKey3:    []byte(aesKey),
		VideoSize3: videoSize,
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal video proto failed: %w", err)
	}

	// 手动追加proto3不会序列化的0值字段
	// field 14 (unknown14=0)
	data = appendZeroVarintField(data, 14)
	// field 38 (unknown38=0)
	data = appendZeroVarintField(data, 38)

	return hex.EncodeToString(data), nil
}
