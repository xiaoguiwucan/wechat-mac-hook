package main

import (
	"encoding/hex"
	"strings"
	"testing"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/proto"
)

func TestBuildNativeEmojiAppMsgProto(t *testing.T) {
	encoded, err := BuildNativeEmojiAppMsgProto(
		"wxid_sender", "18725461928@chatroom", "a5849c3be9f462774d92657cc3646e5f", 366360,
	)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := hex.DecodeString(encoded)
	if err != nil {
		t.Fatal(err)
	}
	var req wxproto.SendAppMsgReq
	if err := proto.Unmarshal(raw, &req); err != nil {
		t.Fatal(err)
	}
	if req.GetMsg().GetType() != 8 {
		t.Fatalf("type=%d", req.GetMsg().GetType())
	}
	if req.GetMsg().GetToUserName() != "18725461928@chatroom" {
		t.Fatalf("target=%q", req.GetMsg().GetToUserName())
	}
	for _, want := range []string{"<type>8</type>", "<totallen>366360</totallen>", "<emoticonmd5>a5849c3be9f462774d92657cc3646e5f</emoticonmd5>"} {
		if !strings.Contains(req.GetMsg().GetContent(), want) {
			t.Fatalf("content missing %q", want)
		}
	}
}

func TestBuildNativeEmojiAppMsgProtoRejectsInvalidAsset(t *testing.T) {
	if _, err := BuildNativeEmojiAppMsgProto("wxid_sender", "group@chatroom", "bad", 10); err == nil {
		t.Fatal("expected invalid md5 error")
	}
	if _, err := BuildNativeEmojiAppMsgProto("wxid_sender", "group@chatroom", "a5849c3be9f462774d92657cc3646e5f", 0); err == nil {
		t.Fatal("expected invalid length error")
	}
}
