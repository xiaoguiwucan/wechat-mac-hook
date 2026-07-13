package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const tinyPlaceholderPNG = "base64://iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lD3X9wAAAABJRU5ErkJggg=="

var mediaProbeMu sync.Mutex
var mediaProbeState = map[string]any{
	"ready":           false,
	"running":         false,
	"last_attempt":    "",
	"last_success":    "",
	"last_failure":    "",
	"last_reason":     "",
	"last_error":      "",
	"last_target":     "",
	"success_count":   0,
	"failure_count":   0,
	"last_latency_ms": 0,
}
var lastMediaProbeAttempt time.Time
var lastMediaProbeReport = map[string]string{}

func envBool(name string, def bool) bool {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	switch strings.ToLower(v) {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return def
	}
}

func envInt(name string, def int) int {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func mediaProbeSnapshot() map[string]any {
	mediaProbeMu.Lock()
	defer mediaProbeMu.Unlock()
	out := make(map[string]any, len(mediaProbeState))
	for k, v := range mediaProbeState {
		out[k] = v
	}
	out["enabled"] = config.MediaProbeEnabled
	out["target"] = config.MediaProbeTarget
	out["interval_seconds"] = config.MediaProbeIntervalSeconds
	out["cooldown_seconds"] = config.MediaProbeCooldownSeconds
	return out
}

func updateMediaProbe(fields map[string]any) map[string]any {
	mediaProbeMu.Lock()
	defer mediaProbeMu.Unlock()
	for k, v := range fields {
		mediaProbeState[k] = v
	}
	out := make(map[string]any, len(mediaProbeState))
	for k, v := range mediaProbeState {
		out[k] = v
	}
	return out
}

func markMediaChannelSuccess(reason string) {
	now := time.Now().Format(time.RFC3339)
	snap := updateMediaProbe(map[string]any{
		"ready":         true,
		"running":       false,
		"last_success":  now,
		"last_reason":   reason,
		"last_error":    "",
		"success_count": intValue(mediaProbeState["success_count"]) + 1,
	})
	reportMediaProbeState("ok", reason, snap)
}

func markMediaChannelFailure(reason string, err error) {
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	now := time.Now().Format(time.RFC3339)
	snap := updateMediaProbe(map[string]any{
		"ready":         false,
		"running":       false,
		"last_failure":  now,
		"last_reason":   reason,
		"last_error":    msg,
		"failure_count": intValue(mediaProbeState["failure_count"]) + 1,
	})
	reportMediaProbeState("failed", reason, snap)
	if config.MediaProbeEnabled {
		go func() {
			_, _ = runMediaProbe("auto_after_failure:"+reason, false)
		}()
	}
}

func intValue(v any) int {
	switch x := v.(type) {
	case int:
		return x
	case int64:
		return int(x)
	case float64:
		return int(x)
	default:
		return 0
	}
}

func isMediaTask(t string) bool {
	switch t {
	case "image", "send_image", "video", "send_video", "voice", "send_voice", "file", "send_file_simple":
		return true
	default:
		return false
	}
}

func isFinalMediaTask(t string) bool {
	switch t {
	case "send_image", "send_video", "send_voice", "send_file_simple":
		return true
	default:
		return false
	}
}

func fridaHookState() map[string]any {
	out := map[string]any{"loaded": fridaScript != nil}
	if fridaScript == nil {
		return out
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ret := fridaScript.ExportsCallWithContext(ctx, "getUploadStatus")
	if ret == nil {
		out["error"] = "empty frida rpc result"
		return out
	}
	s := fmt.Sprint(ret)
	var parsed map[string]any
	if err := json.Unmarshal([]byte(s), &parsed); err != nil {
		out["error"] = "parse frida rpc result: " + err.Error()
		out["raw"] = s
		return out
	}
	return parsed
}

func resetUploadChannelInFrida(reason string) {
	if fridaScript == nil {
		return
	}
	// 不再调用 resetUploadChannel（JS 侧已移除）
	// 改为调用 recoverUploadX0，尝试纯后台恢复
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ret := fridaScript.ExportsCallWithContext(ctx, "recoverUploadX0", "filehelper")
	if ret != nil {
		Info("[媒体探针] recoverUploadX0 调用成功", "reason", reason, "ret", fmt.Sprint(ret))
	} else {
		Warn("[媒体探针] recoverUploadX0 调用失败", "reason", reason)
	}
}

// waitForUploadX0 轮询 getUploadStatus，等待 upload_x0_ready=true
// 最多等待 maxWait 秒，每 500ms 检查一次
func waitForUploadX0(maxWait time.Duration) bool {
	deadline := time.Now().Add(maxWait)
	for time.Now().Before(deadline) {
		if fridaScript == nil {
			return false
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		ret := fridaScript.ExportsCallWithContext(ctx, "getUploadStatus")
		cancel()
		if ret != nil {
			s := fmt.Sprint(ret)
			var parsed map[string]any
			if err := json.Unmarshal([]byte(s), &parsed); err == nil {
				if ready, _ := parsed["upload_x0_ready"].(bool); ready {
					return true
				}
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

func runMediaProbe(reason string, force bool) (map[string]any, error) {
	if !config.MediaProbeEnabled && !force {
		return map[string]any{"started": false, "skipped": "disabled", "media": mediaProbeSnapshot(), "frida": fridaHookState()}, nil
	}
	now := time.Now()
	mediaProbeMu.Lock()
	if running, _ := mediaProbeState["running"].(bool); running {
		snap := make(map[string]any, len(mediaProbeState))
		for k, v := range mediaProbeState {
			snap[k] = v
		}
		mediaProbeMu.Unlock()
		return map[string]any{"started": false, "skipped": "already_running", "media": snap, "frida": fridaHookState()}, nil
	}
	if !force && !lastMediaProbeAttempt.IsZero() && now.Sub(lastMediaProbeAttempt) < time.Duration(config.MediaProbeCooldownSeconds)*time.Second {
		snap := make(map[string]any, len(mediaProbeState))
		for k, v := range mediaProbeState {
			snap[k] = v
		}
		mediaProbeMu.Unlock()
		return map[string]any{"started": false, "skipped": "cooldown", "media": snap, "frida": fridaHookState()}, nil
	}
	lastMediaProbeAttempt = now
	mediaProbeState["running"] = true
	mediaProbeState["last_attempt"] = now.Format(time.RFC3339)
	mediaProbeState["last_reason"] = reason
	mediaProbeState["last_error"] = ""
	mediaProbeState["last_target"] = config.MediaProbeTarget
	mediaProbeMu.Unlock()

	started := time.Now()
	state := fridaHookState()
	uploadReady, _ := state["upload_x0_ready"].(bool)
	if !uploadReady {
		resetUploadChannelInFrida("probe_precheck:" + reason)
	}
	if sendReady, _ := state["send_ready"].(bool); !sendReady {
		err := fmt.Errorf("StartTask 发送通道未就绪")
		snap := updateMediaProbe(map[string]any{
			"ready":         false,
			"running":       false,
			"last_failure":  time.Now().Format(time.RFC3339),
			"last_error":    err.Error(),
			"failure_count": intValue(mediaProbeState["failure_count"]) + 1,
		})
		reportMediaProbeState("failed", reason, snap)
		return map[string]any{"started": false, "ready": false, "error": err.Error(), "media": snap, "frida": state}, err
	}

	ch := make(chan error, 1)
	msg := &SendMsg{
		UserId:     config.MediaProbeTarget,
		Content:    tinyPlaceholderPNG,
		Type:       "image",
		ResultChan: ch,
	}
	select {
	case msgChan <- msg:
	case <-time.After(3 * time.Second):
		err := fmt.Errorf("发送队列繁忙，探针入队超时")
		snap := updateMediaProbe(map[string]any{"ready": false, "running": false, "last_failure": time.Now().Format(time.RFC3339), "last_error": err.Error(), "failure_count": intValue(mediaProbeState["failure_count"]) + 1})
		reportMediaProbeState("failed", reason, snap)
		return map[string]any{"started": false, "ready": false, "error": err.Error(), "media": snap, "frida": state}, err
	}

	var err error
	// 如果 X0 之前未就绪，发送占位图后等待 hook 捕获 X0
	wasX0Missing := !uploadReady
	select {
	case err = <-ch:
	case <-time.After(time.Duration(config.MediaProbeTimeoutSeconds) * time.Second):
		err = fmt.Errorf("媒体探针超时")
	}

	// 即使发送失败，也检查 X0 是否已被 hook 捕获
	if wasX0Missing {
		if waitForUploadX0(8 * time.Second) {
			Info("[媒体探针] X0 已通过发送占位图恢复", "reason", reason)
			err = nil
		} else {
			Warn("[媒体探针] 发送占位图后 X0 仍未恢复", "reason", reason)
		}
	}

	latency := int(time.Since(started).Milliseconds())
	if err != nil {
		resetUploadChannelInFrida("probe_failed:" + reason)
		snap := updateMediaProbe(map[string]any{
			"ready":           false,
			"running":         false,
			"last_failure":    time.Now().Format(time.RFC3339),
			"last_error":      err.Error(),
			"last_latency_ms": latency,
			"failure_count":   intValue(mediaProbeState["failure_count"]) + 1,
		})
		reportMediaProbeState("failed", reason, snap)
		return map[string]any{"started": true, "ready": false, "error": err.Error(), "media": snap, "frida": fridaHookState()}, err
	}
	snap := updateMediaProbe(map[string]any{
		"ready":           true,
		"running":         false,
		"last_success":    time.Now().Format(time.RFC3339),
		"last_error":      "",
		"last_latency_ms": latency,
		"success_count":   intValue(mediaProbeState["success_count"]) + 1,
	})
	reportMediaProbeState("ok", reason, snap)
	return map[string]any{"started": true, "ready": true, "media": snap, "frida": fridaHookState()}, nil
}

func mediaProbeLoop() {
	if !config.MediaProbeEnabled {
		return
	}
	time.Sleep(5 * time.Second)
	for {
		state := fridaHookState()
		mediaReady, _ := mediaProbeSnapshot()["ready"].(bool)
		uploadReady, _ := state["upload_x0_ready"].(bool)
		if !mediaReady || !uploadReady {
			_, _ = runMediaProbe("self_check", false)
		}
		interval := config.MediaProbeIntervalSeconds
		if interval < 15 {
			interval = 15
		}
		time.Sleep(time.Duration(interval) * time.Second)
	}
}

func onebotHealthHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "ok",
		"time":       time.Now().Format(time.RFC3339),
		"wechat_pid": currentWechatPid,
		"self_id":    myWechatId,
		"frida":      fridaHookState(),
		"media":      mediaProbeSnapshot(),
	})
}

func mediaProbeHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		http.Error(w, "仅支持 GET/POST", http.StatusMethodNotAllowed)
		return
	}
	reason := r.URL.Query().Get("reason")
	if reason == "" {
		reason = "api"
	}
	force := r.URL.Query().Get("force") != "0"
	result, err := runMediaProbe(reason, force)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"status": "failed", "error": err.Error(), "result": result})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "result": result})
}

func writeJSON(w http.ResponseWriter, code int, obj any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(obj)
}

func reportMediaProbeState(subType string, reason string, snapshot map[string]any) {
	key := subType + "|" + reason + "|" + fmt.Sprint(snapshot["last_error"])
	if lastMediaProbeReport[reason] == key && subType == "failed" {
		return
	}
	lastMediaProbeReport[reason] = key
	payload := map[string]any{
		"post_type":       "meta_event",
		"meta_event_type": "media_channel",
		"sub_type":        subType,
		"time":            time.Now().Unix(),
		"self_id":         myWechatId,
		"wechat_pid":      currentWechatPid,
		"reason":          reason,
		"media":           snapshot,
	}
	go postMetaEvent(payload)
}

func postMetaEvent(payload map[string]any) {
	if strings.TrimSpace(config.SendURL) == "" {
		return
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return
	}
	req, err := http.NewRequest("POST", config.SendURL, bytes.NewReader(raw))
	if err != nil {
		return
	}
	h := hmac.New(sha1.New, []byte(config.OnebotToken))
	h.Write(raw)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Signature", "sha1="+hex.EncodeToString(h.Sum(nil)))
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		Warn("媒体通道状态上报失败", "err", err)
		return
	}
	_ = resp.Body.Close()
}
