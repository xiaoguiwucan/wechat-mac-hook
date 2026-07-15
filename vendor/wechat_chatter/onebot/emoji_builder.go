package main

import (
	"encoding/hex"
	"fmt"
	"regexp"
	"time"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/proto"
)

var nativeEmojiMD5Pattern = regexp.MustCompile(`^[0-9a-f]{32}$`)

// BuildNativeEmojiAppMsgProto builds WeChat's native type=8 emoticon message.
// The referenced GIF already exists in WeChat's emoticon cache/favourites, so
// this path only sends its stable MD5 and length and never uploads/re-encodes
// the image. This preserves animation and remains usable after OneBot restarts.
func BuildNativeEmojiAppMsgProto(sender, receiver, md5 string, totalLen int32) (string, error) {
	if sender == "" || receiver == "" {
		return "", fmt.Errorf("emoji sender/receiver is empty")
	}
	if !nativeEmojiMD5Pattern.MatchString(md5) {
		return "", fmt.Errorf("invalid emoji md5")
	}
	if totalLen <= 0 || totalLen > 25*1024*1024 {
		return "", fmt.Errorf("invalid emoji length: %d", totalLen)
	}

	now := time.Now()
	xml := `<appmsg appid="" sdkver="0">` +
		`<title></title><des></des><action></action><type>8</type>` +
		`<showtype>0</showtype><soundtype>0</soundtype>` +
		`<mediatagname></mediatagname><messageext></messageext>` +
		`<messageaction></messageaction><content></content><contentattr>0</contentattr>` +
		`<url></url><lowurl></lowurl><dataurl></dataurl><lowdataurl></lowdataurl>` +
		`<songalbumurl></songalbumurl><songlyric></songlyric><appattach>` +
		fmt.Sprintf(`<totallen>%d</totallen><attachid>0:0:%s</attachid><emoticonmd5>%s</emoticonmd5>`, totalLen, md5, md5) +
		`<fileext>pic</fileext><cdnthumbaeskey></cdnthumbaeskey><aeskey></aeskey>` +
		`</appattach><extinfo></extinfo><sourceusername></sourceusername>` +
		`<sourcedisplayname></sourcedisplayname><thumburl></thumburl><md5></md5>` +
		`<statextstr></statextstr><directshare>0</directshare></appmsg>` +
		`<fromusername>` + sender + `</fromusername>`

	version := NextVersion()
	appID := ""
	sdkVersion := uint32(0)
	msgType := uint32(8)
	clientMsgID := fmt.Sprintf("%d", now.UnixMilli())
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
			AppId:        &appID,
			SdkVersion:   &sdkVersion,
			ToUserName:   &receiver,
			Type:         &msgType,
			Content:      &xml,
			CreateTime:   proto.Int64(now.Unix()),
			ClientMsgId:  &clientMsgID,
			MsgSource:    &msgSource,
		},
	}
	data, err := proto.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal native emoji appmsg failed: %w", err)
	}
	return hex.EncodeToString(data), nil
}
