package main

import "testing"

func TestResolveMentionPrefersProvidedReadableName(t *testing.T) {
	data := &SendRequestData{QQ: "wxid_8f3s1m3giuy022", Name: "粉嘟嘟."}
	userID, displayName, ok := resolveMention("18725461928@chatroom", data)
	if !ok || userID != data.QQ || displayName != data.Name {
		t.Fatalf("unexpected mention result: user=%q name=%q ok=%v", userID, displayName, ok)
	}
}

func TestResolveMentionRejectsInternalAndChatroomNames(t *testing.T) {
	groupID := "18725461928@chatroom"
	userID := "wxid_8f3s1m3giuy022"
	userID2NicknameMap.Store(groupID+"_"+userID, userID)
	t.Cleanup(func() { userID2NicknameMap.Delete(groupID + "_" + userID) })
	if _, _, ok := resolveMention(groupID, &SendRequestData{QQ: userID, Name: userID}); ok {
		t.Fatal("wxid must never be rendered as a visible mention")
	}
	if _, _, ok := resolveMention(groupID, &SendRequestData{QQ: groupID, Name: "测试群"}); ok {
		t.Fatal("chatroom id must never be treated as a member mention")
	}
}

func TestResolveMentionUsesReadableCacheAsFallback(t *testing.T) {
	groupID := "18725461928@chatroom"
	userID := "saarjoye"
	userID2NicknameMap.Store(groupID+"_"+userID, "姆巴佩")
	t.Cleanup(func() { userID2NicknameMap.Delete(groupID + "_" + userID) })
	gotUserID, gotName, ok := resolveMention(groupID, &SendRequestData{UserID: userID})
	if !ok || gotUserID != userID || gotName != "姆巴佩" {
		t.Fatalf("unexpected cached mention: user=%q name=%q ok=%v", gotUserID, gotName, ok)
	}
}

func TestExtractGroupSenderUserSupportsLegacyUsername(t *testing.T) {
	groupID := "18725461928@chatroom"
	if got := extractGroupSenderUser("saarjoye:\n姆巴佩回家了？", groupID); got != "saarjoye" {
		t.Fatalf("got %q, want legacy username", got)
	}
	if got := extractGroupSenderUser("普通消息，没有发送者前缀", groupID); got != groupID {
		t.Fatalf("invalid prefix should preserve fallback, got %q", got)
	}
}
