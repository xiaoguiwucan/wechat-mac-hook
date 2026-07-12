package main

import (
	"encoding/hex"
	"fmt"
	"time"

	"github.com/google/uuid"
	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/proto"
)

// BuildVoiceMsgProto 构建发送语音消息的protobuf并返回hex编码的字符串
func BuildVoiceMsgProto(sender, targetId, cdnKey, aesKey string, voiceDuration int32, silkDataLen int32, unknown13 int32) (string, error) {
	// client_msg_id: UUID格式
	clientMsgId := uuid.New().String()

	msg := &wxproto.WxSendVoiceMsg{
		FromUser:    sender,
		ToUser:      targetId,
		Unknown3:    0,
		Unknown4:    silkDataLen,
		ClientMsgId: clientMsgId,
		Duration:    voiceDuration,
		Unknown9:    1,
		Header: &wxproto.VoiceMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   int64(globalSessionId),
			ClientProof: globalClientProof,
			DeviceId:    int64(globalDeviceId),
			Platform:    []byte("UnifiedPCMac 26 arm64"),
			Version:     int32(NextVersion()),
			Unknown8:    4,
			Unknown15:   0,
		},
		Unknown11: 0,
		Unknown13: 4,
		Unknown15: 0,
		Unknown16: 0,
		Unknown17: time.Now().Unix(),
		CdnKey:    []byte(cdnKey),
		AesKey:    []byte(aesKey),
		Unknown24: []byte{},
		Unknown25: 0,
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal voice proto failed: %w", err)
	}

	// 手动追加proto3不会序列化的0值字段
	// field 3 (unknown3=0)
	data = appendZeroVarintField(data, 3)
	// field 11 (unknown11=0)
	data = appendZeroVarintField(data, 11)
	// field 15 (unknown15=0)
	data = appendZeroVarintField(data, 15)
	// field 16 (unknown16=0)
	data = appendZeroVarintField(data, 16)
	// field 25 (unknown25=0)
	data = appendZeroVarintField(data, 25)

	//fmt.Printf("[voice-proto] final protobuf hex dump:\n%s\n", HexDump(data, 0))

	return hex.EncodeToString(data), nil
}
