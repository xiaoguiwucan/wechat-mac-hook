package main

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"time"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// BuildImgMsgProto 构建发送图片消息的protobuf并返回hex编码的字符串
func BuildImgMsgProto(sender, targetId, cdnKey, aesKey, md5Key string) (string, error) {
	// client_msg_id: targetId_timestamp_160_xwechat_3
	timestamp := time.Now().Unix()
	clientMsgId := targetId + "_" + strconv.FormatInt(timestamp, 10) + "_160_xwechat_3"

	// xml内容
	xmlContent := []byte("<msgsource><alnode><fr>1</fr></alnode></msgsource>")

	msg := &wxproto.WxSendImgMsg{
		Header: &wxproto.ImgMsgHeader{
			Flag:        []byte{0x00},
			SessionId:   int64(globalSessionId),
			ClientProof: globalClientProof,
			DeviceId:    int64(globalDeviceId),
			Platform:    []byte("UnifiedPCMac 26 arm64"),
			Version:     304,
		},
		ClientMsgId: &wxproto.WxString{Value: clientMsgId},
		Sender:      &wxproto.WxString{Value: sender},
		Receiver:    &wxproto.WxString{Value: targetId},
		Unknown5:    1524,
		// MsgType: 0, // proto3不会序列化0值，需要手动追加
		Unknown7: 1524,
		// Unknown8: 手动追加 [0x42, 0x04, 0x08, 0x00, 0x12, 0x00]
		Unknown9:  3,
		Xml:       xmlContent,
		Unknown11: 1,
		Unknown12: 2,
		// Unknown13: 0, // 需要手动追加
		CdnKey:    []byte(cdnKey),
		CdnKey2:   []byte(cdnKey),
		AesKey:    []byte(aesKey),
		Unknown18: 1,
		Unknown19: 2559,
		Unknown20: 2559,
		CdnKey3:   []byte(cdnKey),
		Unknown22: 1524,
		Unknown23: 104,
		Unknown24: 58,
		AesKey2:   []byte(aesKey),
		Md5Key:    []byte(md5Key),
		Unknown28: 779219929,
		// Unknown30: 0, // 需要手动追加
		// Unknown36: 0, // 需要手动追加
		// Unknown41: 0, // 需要手动追加
	}

	data, err := proto.Marshal(msg)
	if err != nil {
		return "", fmt.Errorf("marshal img proto failed: %w", err)
	}

	// 手动追加proto3不会序列化的0值字段
	// field 6 (msg_type=0): tag=0x30, value=0x00
	data = appendZeroVarintField(data, 6)
	// field 8 (ImgMsgExtra with field1=0, field2=""): [0x42, 0x04, 0x08, 0x00, 0x12, 0x00]
	data = append(data, 0x42, 0x04, 0x08, 0x00, 0x12, 0x00)
	// field 13 (unknown13=0): tag=0x68, value=0x00
	data = appendZeroVarintField(data, 13)
	// field 30 (unknown30=0): tag=[0xF0, 0x01], value=0x00
	data = appendZeroVarintField(data, 30)
	// field 36 (unknown36=0): tag=[0xA0, 0x02], value=0x00
	data = appendZeroVarintField(data, 36)
	// field 41 (unknown41=0): tag=[0xC8, 0x02], value=0x00
	data = appendZeroVarintField(data, 41)

	// 追加尾部的 \x00 字节
	data = append(data, 0x00)

	return hex.EncodeToString(data), nil
}

// appendZeroVarintField 追加一个值为0的varint字段
func appendZeroVarintField(data []byte, fieldNum protowire.Number) []byte {
	data = protowire.AppendTag(data, fieldNum, protowire.VarintType)
	data = protowire.AppendVarint(data, 0)
	return data
}
