package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	_ "net/http/pprof"
	"os"
	"os/signal"
	"runtime/debug"
	"strings"
	"syscall"
	"text/template"
	"time"

	"github.com/frida/frida-go/frida"
)

func main() {
	initFlag()
	initLogger()
	if config.FridaType == "gadget" {
		initFridaGadget()
	} else {
		initFrida()
	}
	go SendWorker()
	go cleanExpiredDownloads()

	http.HandleFunc("/send_private_msg", sendHandler)
	http.HandleFunc("/send_group_msg", sendHandler)

	http.HandleFunc("/ws", handleWebSocket)
	http.HandleFunc("/test_ws", testWebSocket)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		<-stop
		fridaScript.Clean()
		session.Clean()
		device.Clean()
		Fatal("正在释放 Frida 资源并退出...")
	}()

	// 3. 启动服务
	Info("HTTP 服务启动在", "host", config.ReceiveHost)
	if err := http.ListenAndServe(config.ReceiveHost, nil); err != nil {
		Error("服务启动失败", "err", err)
	}

}

func initFlag() {
	flag.StringVar(&config.FridaType, "type", "local", "frida 类型: local | gadget")
	flag.StringVar(&config.SendURL, "send_url", "http://127.0.0.1:36060/onebot", "发送消息的 URL: http://127.0.0.1:36060/onebot")
	flag.StringVar(&config.ReceiveHost, "receive_host", "127.0.0.1:58080", "接收消息的地址: 127.0.0.1:58080")
	flag.StringVar(&config.FridaGadgetAddr, "gadget_addr", "127.0.0.1:27042", "Gadget 地址: 127.0.0.1:27042 仅当 type 为 gadget 时有效")
	flag.StringVar(&config.OnebotToken, "token", "MuseBot", "OneBot Token: MuseBot")
	flag.StringVar(&config.ImagePath, "image_path", "", "图片路径: /Users/xxx/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/xxx/temp/xxx/2026-01/Img/")
	flag.StringVar(&config.WechatConf, "wechat_conf", "../wechat_version/4_1_11_53_mac.json", "微信配置文件路径: ../wechat_version/4_1_6_12_mac.json")
	flag.StringVar(&config.ConnType, "conn_type", "http", "连接类型: http | websocket")
	flag.IntVar(&config.SendInterval, "send_interval", 1000, "发送间隔: ms")
	flag.IntVar(&config.WechatPid, "wechat_pid", 0, "微信进程 PID，不设置则自动查找")
	flag.StringVar(&logLevel, "log_level", "info", "log level")

	flag.Parse()

	if myWechatId == "" && config.ImagePath != "" {
		if idx := strings.Index(config.ImagePath, "xwechat_files/"); idx != -1 {
			rest := config.ImagePath[idx+len("xwechat_files/"):]
			if end := strings.Index(rest, "/"); end != -1 {
				rest = rest[:end]
			}
			// 去掉末尾的 "_xxxx" 后缀，保留 wxid_xxx 部分
			if last := strings.LastIndex(rest, "_"); last > strings.Index(rest, "_") {
				myWechatId = rest[:last]
			}
		}
	}

	fmt.Println("FridaType", config.FridaType)
	fmt.Println("SendURL", config.SendURL)
	fmt.Println("ReceiveHost", config.ReceiveHost)
	fmt.Println("FridaGadgetAddr", config.FridaGadgetAddr)
	fmt.Println("OnebotToken", config.OnebotToken)
	fmt.Println("ImagePath", config.ImagePath)
	fmt.Println("WechatConf", config.WechatConf)
	fmt.Println("ConnType", config.ConnType)
	fmt.Println("SendInterval", config.SendInterval)
	fmt.Println("WechatPid", config.WechatPid)
	fmt.Println("LogLevel", logLevel)
}

func initFridaGadget() {
	var err error
	mgr := frida.NewDeviceManager()
	// 连接到 Gadget 默认端口
	device, err = mgr.AddRemoteDevice(config.FridaGadgetAddr, frida.NewRemoteDeviceOptions())
	if err != nil {
		Fatal("❌ 无法连接 Gadget", err)
	}

	session, err = device.Attach("Gadget", nil)
	if err != nil {
		Fatal("❌ 附加失败", err)
	}

	loadJs()

}

func initFrida() {
	var err error
	// 1. 获取本地设备管理器
	mgr := frida.NewDeviceManager()

	// 2. 枚举并获取本地设备 (TypeLocal)
	device, err = mgr.DeviceByType(frida.DeviceTypeLocal)
	if err != nil {
		Fatal("无法获取本地设备", "err", err)
	}

	attachWechat()
}

func attachWechat() {
	var pid int
	var err error
	if config.WechatPid > 0 {
		pid = config.WechatPid
		Info("使用指定的微信进程 PID", "PID", pid)
	} else {
		for {
			pid, err = GetWeChatPID()
			if err == nil {
				break
			}
			Info("未发现正在运行的微信进程，20秒后重试...")
			time.Sleep(20 * time.Second)
		}
		Info("自动发现微信进程 PID", "PID", pid)
	}

	session, err = device.Attach(pid, nil)
	if err != nil {
		Fatal("Attach 失败 (请检查 SIP 状态或权限)", "err", err)
	}
	Info("成功 Attach 微信进程", "PID", pid)

	loadJs()
	MonitorProcess(pid)
}

func loadJs() {
	jsonData, err := os.ReadFile(config.WechatConf)
	if err != nil {
		Fatal("读取文件失败", "err", err)
	}

	// 2. 将 JSON 解析为 Map
	var wechatHookConf map[string]interface{}
	if err = json.Unmarshal(jsonData, &wechatHookConf); err != nil {
		Fatal("解析 JSON 失败", "err", err)
	}

	codeTemplate, err := os.ReadFile("./script.js")
	if err != nil {
		Fatal("读取脚本失败", "err", err)
	}

	tmpl, err := template.New("fridaScript").Parse(string(codeTemplate))
	if err != nil {
		Fatal("解析模板失败", "err", err)
		return
	}

	var buf bytes.Buffer
	err = tmpl.Execute(&buf, wechatHookConf)
	if err != nil {
		Fatal("执行模板失败", "err", err)
	}

	script, err := session.CreateScript(buf.String())
	if err != nil {
		Fatal("创建脚本失败", "err", err)
	}

	// 打印 JS 里的 console.log
	script.On("message", func(rawMsg string) {
		defer func() {
			if r := recover(); r != nil {
				Error("message panic", "err", r, "stack", string(debug.Stack()))
			}
		}()

		var msg map[string]interface{}
		err = json.Unmarshal([]byte(rawMsg), &msg)
		if err != nil {
			Error("JSON解析失败", "err", err)
			return
		}

		msgType := msg["type"].(string)

		switch msgType {
		case "send":
			if p, ok := msg["payload"]; ok {
				if pMap, ok := p.(map[string]interface{}); ok {
					payloadJson, _ := json.Marshal(pMap)
					if t, ok := pMap["type"]; ok {
						switch t.(string) {
						case "protobuf_msg":
							go HandleProtobufMsgAndSend(pMap)
						case "send":
							if config.ConnType == "http" {
								go SendHttpReq(payloadJson)
							} else {
								go SendWebSocketMsg(payloadJson)
							}
						case "buf2resp":
							go func() {
								msgType := ""
								if mt, ok := pMap["msg_type"]; ok {
									msgType = mt.(string)
								}
								if dataInter, ok := pMap["data"]; ok {
									if dataArr, ok := dataInter.([]interface{}); ok {
										rawBytes := make([]byte, len(dataArr))
										for i, v := range dataArr {
											if f, ok := v.(float64); ok {
												rawBytes[i] = byte(int(f))
											}
										}
										HandleBuf2Resp(msgType, rawBytes)
									}
								}
							}()
						case "upload_image_finish":
							m := &SendMsg{
								Type: "send_image",
							}
							targetId := ""
							if targetIdInter, ok := pMap["target_id"]; ok {
								targetId = targetIdInter.(string)
								if strings.Contains(targetId, "wxid_") {
									m.UserId = targetId
								} else {
									m.GroupID = targetId
								}
							}
							if cdnKey, ok := pMap["cdn_key"]; ok {
								m.CdnKey = cdnKey.(string)
							}
							if aesKey, ok := pMap["aes_key"]; ok {
								m.AesKey = aesKey.(string)
							}
							if md5Key, ok := pMap["md5_key"]; ok {
								m.Md5Key = md5Key.(string)
							}
							if ch, ok := pendingResultMap.LoadAndDelete(targetId); ok {
								m.ResultChan = ch.(chan error)
							}
							msgChan <- m
						case "upload_video_finish":
							m := &SendMsg{
								Type: "send_video",
							}
							targetId := ""
							if targetIdInter, ok := pMap["target_id"]; ok {
								targetId = targetIdInter.(string)
								if strings.Contains(targetId, "wxid_") {
									m.UserId = targetId
								} else {
									m.GroupID = targetId
								}
							}
							if cdnKey, ok := pMap["cdn_key"]; ok {
								m.CdnKey = cdnKey.(string)
							}
							if aesKey, ok := pMap["aes_key"]; ok {
								m.AesKey = aesKey.(string)
							}
							if md5Key, ok := pMap["md5_key"]; ok {
								m.Md5Key = md5Key.(string)
							}
							if videoId, ok := pMap["video_id"]; ok {
								m.VideoId = videoId.(string)
							}
							if ch, ok := pendingResultMap.LoadAndDelete(targetId); ok {
								m.ResultChan = ch.(chan error)
							}
							msgChan <- m
						case "upload_voice_finish":
							m := &SendMsg{
								Type: "send_voice",
							}
							targetId := ""
							if targetIdInter, ok := pMap["target_id"]; ok {
								targetId = targetIdInter.(string)
								if strings.Contains(targetId, "wxid_") {
									m.UserId = targetId
								} else {
									m.GroupID = targetId
								}
							}
							if cdnKey, ok := pMap["cdn_key"]; ok {
								m.CdnKey = cdnKey.(string)
							}
							if aesKey, ok := pMap["aes_key"]; ok {
								m.AesKey = aesKey.(string)
							}
							if voiceDuration, ok := pMap["voice_duration"]; ok {
								if vd, ok := voiceDuration.(float64); ok {
									m.VoiceDuration = int32(vd)
								}
							}
							if silkDataLen, ok := pMap["silk_data_len"]; ok {
								if sdl, ok := silkDataLen.(float64); ok {
									m.SilkDataLen = int32(sdl)
								}
							}
							if ch, ok := pendingResultMap.LoadAndDelete(targetId); ok {
								m.ResultChan = ch.(chan error)
							}
							msgChan <- m
						case "download":
							err = Download(payloadJson)
							if err != nil {
								Error("下载失败", "err", err)
							}
						}

					}
				}
			}
		case "log":
			Info("[JS日志]", "payload", msg["payload"])
		case "error":
			Error("[JS日志报错]", "err", msg["description"], "stack", msg["stack"])
		}
	})

	if err := script.Load(); err != nil {
		Fatal("❌ 加载脚本失败", err)
	}

	fridaScript = script
	Info("✅ Frida 已就绪，微信控制通道已打通")
}
