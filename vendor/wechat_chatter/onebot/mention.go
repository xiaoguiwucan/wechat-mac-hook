package main

import (
	"regexp"
	"strings"
)

var groupSenderIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.@-]{2,128}$`)

// extractGroupSenderUser repairs group events where the protobuf sender field
// contains the chatroom id. WeChat still prefixes the message body with the
// real member account, including legacy usernames that do not start with wxid_.
func extractGroupSenderUser(content, fallback string) string {
	if splitIndex := strings.Index(content, ":"); splitIndex > 0 {
		candidate := strings.TrimSpace(content[:splitIndex])
		if groupSenderIDPattern.MatchString(candidate) && !strings.HasSuffix(candidate, "@chatroom") {
			return candidate
		}
	}
	return fallback
}

func readableMentionName(value, userID string) bool {
	name := strings.TrimSpace(value)
	lower := strings.ToLower(name)
	if name == "" || name == strings.TrimSpace(userID) || strings.HasPrefix(lower, "wxid_") || strings.HasSuffix(lower, "@chatroom") {
		return false
	}
	switch lower {
	case "群友", "群成员", "未知成员", "未知发送者", "unknown", "member":
		return false
	}
	return true
}

// resolveMention keeps the machine user id for the native at-list while using
// a verified human-readable name in the visible message. If no readable name
// exists, the mention is skipped instead of leaking wxid/chatroom identifiers.
func resolveMention(groupID string, data *SendRequestData) (string, string, bool) {
	if data == nil {
		return "", "", false
	}
	userID := strings.TrimSpace(data.QQ)
	if userID == "" {
		userID = strings.TrimSpace(data.UserID)
	}
	if userID == "" || strings.HasSuffix(userID, "@chatroom") {
		return "", "", false
	}
	if readableMentionName(data.Name, userID) {
		return userID, strings.TrimSpace(data.Name), true
	}
	if cached, ok := userID2NicknameMap.Load(groupID + "_" + userID); ok {
		if name, ok := cached.(string); ok && readableMentionName(name, userID) {
			return userID, strings.TrimSpace(name), true
		}
	}
	return "", "", false
}
