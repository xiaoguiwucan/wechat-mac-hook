package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/frida/frida-go/frida"
)

var (
	wechatPID    = flag.Int("wechat-pid", 0, "second WeChat main process PID")
	uiPID        = flag.Int("ui-pid", 0, "optional WeChatAppEx child PID owned by the second WeChat")
	rendererPIDs = flag.String("renderer-pids", "", "comma-separated WeChatAppEx renderer PIDs owned by the second WeChat")
	ocrBin       = flag.String("ocr-bin", "", "local second-WeChat window OCR observer")
	callback     = flag.String("callback", "http://127.0.0.1:36060/onebot", "AI reply callback URL")
	logPath      = flag.String("onebot-log", "", "OneBot runtime log used for record correlation")
)

var recordLine = regexp.MustCompile(`记录最近语音泡.*group_id=([^\s]+).*message_id=([^\s]+)`)
var hasHan = regexp.MustCompile(`[\p{Han}]`)
var forwardedVoiceMessages sync.Map

const hookScript = `
send({type:'voice_sidecar_ready', phase:'script_loaded'});
var sent = {};
var hooked = {};

function globalExport(name) {
  try { if (Module.findGlobalExportByName) return Module.findGlobalExportByName(name); } catch (_) {}
  try { if (Module.getGlobalExportByName) return Module.getGlobalExportByName(name); } catch (_) {}
  try { return Module.findExportByName(null, name); } catch (_) { return null; }
}

function clean(s) {
  return ('' + s).replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
}

function candidate(s, source) {
  try {
    s = clean(s);
    var n = Array.from(s).length;
    if (n < 4 || n > 160 || !/[\u4e00-\u9fff]/.test(s)) return;
    if (/@chatroom|wxid_|file_id=|https?:\/\//.test(s)) return;
    var noise = ['语音输入','按住说话','松开发送','转文字','语音转文字','在群聊中发了一段语音','微信','WeChat','系统设置','辅助功能','朋友圈','通讯录','收藏','搜索'];
    for (var i = 0; i < noise.length; i++) if (s === noise[i]) return;
    var now = Date.now();
    if (sent[s] && now - sent[s] < 15000) return;
    sent[s] = now;
    send({type:'voice_transcript_candidate', text:s, source:source, ts:now});
  } catch (_) {}
}

function objectText(runtime, value) {
  try {
    if (!value || value.isNull()) return '';
    var cstr = runtime.msg(value, runtime.utf8);
    if (cstr && !cstr.isNull()) return cstr.readUtf8String();
  } catch (_) {}
  // Voice transcript bubbles are normally rendered as NSAttributedString.
  // That object does not implement UTF8String; unwrap its plain string first.
  try {
    var plain = runtime.msg(value, runtime.string);
    if (!plain || plain.isNull()) return '';
    var cstr = runtime.msg(plain, runtime.utf8);
    return cstr && !cstr.isNull() ? cstr.readUtf8String() : '';
  } catch (_) { return ''; }
}

function install(runtime, klass, className, selector) {
  try {
    var sel = runtime.sel(Memory.allocUtf8String(selector));
    var method = runtime.method(klass, sel);
    if (!method || method.isNull()) return false;
    var imp = runtime.imp(method);
    if (!imp || imp.isNull() || hooked[imp.toString()]) return false;
    hooked[imp.toString()] = true;
    Interceptor.attach(imp, { onEnter: function(args) {
      try {
        var value = args[2];
        if (!value || value.isNull()) return;
        var text = objectText(runtime, value);
        if (text) candidate(text, className + ' ' + selector);
      } catch (_) {}
    }});
    return true;
  } catch (_) { return false; }
}

function boot() {
  var getClass = globalExport('objc_getClass');
  var selRegister = globalExport('sel_registerName');
  var getMethod = globalExport('class_getInstanceMethod');
  var getImp = globalExport('method_getImplementation');
  var msgSend = globalExport('objc_msgSend');
  var copyClasses = globalExport('objc_copyClassList');
  var classImage = globalExport('class_getImageName');
  var classNameFn = globalExport('class_getName');
  var freeFn = globalExport('free');
  if (!getClass || !selRegister || !getMethod || !getImp || !msgSend || !copyClasses || !classImage || !classNameFn) {
    console.log('voice sidecar: Objective-C runtime exports unavailable');
    return;
  }
  var runtime = {
    getClass: new NativeFunction(getClass, 'pointer', ['pointer']),
    sel: new NativeFunction(selRegister, 'pointer', ['pointer']),
    method: new NativeFunction(getMethod, 'pointer', ['pointer','pointer']),
    imp: new NativeFunction(getImp, 'pointer', ['pointer']),
    msg: new NativeFunction(msgSend, 'pointer', ['pointer','pointer']),
    image: new NativeFunction(classImage, 'pointer', ['pointer']),
    name: new NativeFunction(classNameFn, 'pointer', ['pointer']),
    utf8: null
  };
  runtime.utf8 = runtime.sel(Memory.allocUtf8String('UTF8String'));
  runtime.string = runtime.sel(Memory.allocUtf8String('string'));
  var selectors = ['setStringValue:','setAttributedStringValue:','setString:','setAttributedString:','setText:','setContent:','initWithString:','initWithString:attributes:'];
  var base = ['NSTextField','NSCell','NSTextView','NSAttributedString','NSMutableAttributedString'];
  var total = 0;
  for (var b = 0; b < base.length; b++) {
    var baseClass = runtime.getClass(Memory.allocUtf8String(base[b]));
    if (!baseClass || baseClass.isNull()) continue;
    for (var bs = 0; bs < selectors.length; bs++) if (install(runtime, baseClass, base[b], selectors[bs])) total++;
  }
  var countPtr = Memory.alloc(4); countPtr.writeU32(0);
  var classes = new NativeFunction(copyClasses, 'pointer', ['pointer'])(countPtr);
  var count = countPtr.readU32();
  for (var i = 0; i < count; i++) {
    try {
      var klass = classes.add(i * Process.pointerSize).readPointer();
      var image = runtime.image(klass);
      var imagePath = image && !image.isNull() ? image.readUtf8String() : '';
      var name = runtime.name(klass);
      var nameText = name && !name.isNull() ? name.readUtf8String() : '';
      if (imagePath.indexOf('/WeChat2.app/') < 0 && !/(WX|WC|MM|Chat|Message|Voice)/.test(nameText)) continue;
      for (var s = 0; s < selectors.length; s++) if (install(runtime, klass, nameText, selectors[s])) total++;
    } catch (_) {}
  }
  if (classes && !classes.isNull() && freeFn) new NativeFunction(freeFn, 'void', ['pointer'])(classes);
  console.log('voice sidecar: installed text render hooks=' + total + ' classes=' + count);
}

boot();
`

func cleanText(value string) string {
	return strings.TrimSpace(strings.Join(strings.Fields(value), " "))
}

func parsePIDs(value string) []int {
	seen := map[int]bool{}
	var pids []int
	for _, part := range strings.Split(value, ",") {
		pid, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || pid <= 0 || seen[pid] {
			continue
		}
		seen[pid] = true
		pids = append(pids, pid)
	}
	return pids
}

func pendingRecord(path string, maxAge time.Duration) (string, string, bool) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", "", false
	}
	lines := strings.Split(string(raw), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := lines[i]
		match := recordLine.FindStringSubmatch(line)
		if len(match) != 3 {
			continue
		}
		if len(line) < len("2006-01-02 15:04:05") {
			continue
		}
		seenAt, err := time.ParseInLocation("2006-01-02 15:04:05", line[:19], time.Local)
		if err != nil || time.Since(seenAt) > maxAge {
			return "", "", false
		}
		return match[1], match[2], true
	}
	return "", "", false
}

func forwardTranscript(text, source string, maxAge time.Duration) {
	text = cleanText(text)
	if text == "" {
		return
	}
	groupID, voiceMessageID, ok := pendingRecord(*logPath, maxAge)
	if !ok {
		return
	}
	key := groupID + "|" + voiceMessageID
	if _, loaded := forwardedVoiceMessages.LoadOrStore(key, true); loaded {
		return
	}
	payload := map[string]interface{}{
		"post_type":               "message",
		"message_type":            "group",
		"group_id":                groupID,
		"time":                    time.Now().UnixMilli(),
		"message_id":              voiceMessageID + "-voice-transcript-sidecar",
		"message":                 []map[string]interface{}{{"type": "text", "data": map[string]string{"text": "[语音转文字] " + text}}},
		"raw_message":             "[语音转文字] " + text,
		"show_content":            text,
		"voice_transcript":        true,
		"voice_transcript_text":   text,
		"voice_message_id":        voiceMessageID,
		"voice_transcript_source": source,
	}
	body, _ := json.Marshal(payload)
	resp, err := http.Post(*callback, "application/json", bytes.NewReader(body))
	if err != nil {
		forwardedVoiceMessages.Delete(key)
		fmt.Printf("forward failed: %v\n", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		forwardedVoiceMessages.Delete(key)
	}
	fmt.Printf("forwarded transcript group=%s message=%s source=%s status=%d text=%q\n", groupID, voiceMessageID, source, resp.StatusCode, text)
}

type ocrMessage struct {
	Type string  `json:"type"`
	Text string  `json:"text"`
	X    float64 `json:"x"`
	Y    float64 `json:"y"`
}

func usableOCRTranscript(message ocrMessage) bool {
	text := cleanText(message.Text)
	if message.Type != "candidate" || message.X < 0.34 || len([]rune(text)) < 2 || len([]rune(text)) > 160 {
		return false
	}
	if !hasHan.MatchString(text) || strings.Contains(text, "语音") || strings.Contains(text, "值班群") || strings.Contains(text, "风：") {
		return false
	}
	return true
}

func startOCRMonitor(binary string, pid int) *exec.Cmd {
	if binary == "" {
		executable, err := os.Executable()
		if err != nil {
			fmt.Printf("window OCR disabled: %v\n", err)
			return nil
		}
		binary = filepath.Join(filepath.Dir(executable), "..", "voice_transcript_ocr", "voice-transcript-ocr")
	}
	if _, err := os.Stat(binary); err != nil {
		fmt.Printf("window OCR disabled: %s (%v)\n", binary, err)
		return nil
	}
	cmd := exec.Command(binary, "--pid", strconv.Itoa(pid), "--watch", "--interval-ms", "1200")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		fmt.Printf("window OCR disabled: %v\n", err)
		return nil
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		fmt.Printf("window OCR disabled: %v\n", err)
		return nil
	}
	if err := cmd.Start(); err != nil {
		fmt.Printf("window OCR disabled: %v\n", err)
		return nil
	}
	go func() {
		scanner := bufio.NewScanner(stdout)
		scanner.Buffer(make([]byte, 4096), 256*1024)
		for scanner.Scan() {
			var message ocrMessage
			if json.Unmarshal(scanner.Bytes(), &message) != nil || !usableOCRTranscript(message) {
				continue
			}
			forwardTranscript(message.Text, fmt.Sprintf("window_ocr x=%.3f y=%.3f", message.X, message.Y), 90*time.Second)
		}
	}()
	go func() {
		scanner := bufio.NewScanner(stderr)
		for scanner.Scan() {
			fmt.Printf("window OCR: %s\n", scanner.Text())
		}
	}()
	go func() {
		if err := cmd.Wait(); err != nil {
			fmt.Printf("window OCR stopped: %v\n", err)
		}
	}()
	fmt.Printf("window OCR observer started pid=%d\n", pid)
	return cmd
}

func main() {
	flag.Parse()
	if *wechatPID <= 0 {
		fmt.Fprintln(os.Stderr, "-wechat-pid is required")
		os.Exit(2)
	}
	if *logPath == "" {
		home, _ := os.UserHomeDir()
		*logPath = filepath.Join(home, "Library", "Application Support", "WeChatSecond", "logs", "onebot-wechat2.log")
	}
	mgr := frida.NewDeviceManager()
	device, err := mgr.DeviceByType(frida.DeviceTypeLocal)
	if err != nil {
		panic(err)
	}
	targets := []int{*wechatPID}
	if *uiPID > 0 && *uiPID != *wechatPID {
		targets = append(targets, *uiPID)
	}
	for _, pid := range parsePIDs(*rendererPIDs) {
		if pid != *wechatPID && pid != *uiPID {
			targets = append(targets, pid)
		}
	}
	scripts := make([]*frida.Script, 0, len(targets))
	sessions := make([]*frida.Session, 0, len(targets))
	for index, targetPID := range targets {
		session, err := device.Attach(targetPID, nil)
		if err != nil {
			if index == 0 {
				panic(err)
			}
			fmt.Printf("voice transcript sidecar skipped child PID=%d: %v\n", targetPID, err)
			continue
		}
		script, err := session.CreateScript(hookScript)
		if err != nil {
			if index == 0 {
				panic(err)
			}
			fmt.Printf("voice transcript sidecar failed to prepare child PID=%d: %v\n", targetPID, err)
			continue
		}
		observedPID := targetPID
		script.On("message", func(raw string) {
			fmt.Printf("frida message pid=%d: %s\n", observedPID, raw)
			var msg struct {
				Type    string `json:"type"`
				Payload struct {
					Type   string `json:"type"`
					Text   string `json:"text"`
					Source string `json:"source"`
				} `json:"payload"`
			}
			if json.Unmarshal([]byte(raw), &msg) != nil {
				return
			}
			if msg.Type == "send" && msg.Payload.Type == "voice_transcript_candidate" {
				forwardTranscript(msg.Payload.Text, fmt.Sprintf("pid=%d %s", observedPID, msg.Payload.Source), 20*time.Minute)
				return
			}
			if msg.Type == "log" {
				fmt.Printf("%s\n", raw)
			}
		})
		if err := script.Load(); err != nil {
			if index == 0 {
				panic(err)
			}
			fmt.Printf("voice transcript sidecar failed to load child PID=%d: %v\n", targetPID, err)
			continue
		}
		sessions = append(sessions, session)
		scripts = append(scripts, script)
		fmt.Printf("voice transcript sidecar attached to WeChat2 PID=%d\n", targetPID)
	}
	if len(scripts) == 0 {
		panic("voice transcript sidecar could not attach to any second-WeChat process")
	}
	ocr := startOCRMonitor(*ocrBin, *wechatPID)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	for _, script := range scripts {
		_ = script.Unload()
	}
	if ocr != nil && ocr.Process != nil {
		_ = ocr.Process.Signal(os.Interrupt)
	}
	_ = sessions
}
