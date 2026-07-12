import Cocoa
import Foundation

let envRoot = ProcessInfo.processInfo.environment["WECHAT_MAC_HOOK_ROOT"]
let sourceRoot = URL(fileURLWithPath: #file).deletingLastPathComponent().deletingLastPathComponent().path
let cwdRoot = FileManager.default.currentDirectoryPath
let ROOT_DIR = envRoot ?? (FileManager.default.fileExists(atPath: cwdRoot + "/web_admin/server.py") ? cwdRoot : sourceRoot)
let CONFIG_PATH = ROOT_DIR + "/config/ai_reply_config.json"
let ENV_PATH = ROOT_DIR + "/config/ai_reply.env"
let LOG_DIR = NSHomeDirectory() + "/Library/Application Support/WeChatSecond/logs"

struct TargetGroup { var name: String; var id: String }

class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var tabView = NSTabView()

    var statusWechat = NSTextField(labelWithString: "WeChat2：未知")
    var statusOneBot = NSTextField(labelWithString: "OneBot：未知")
    var statusAI = NSTextField(labelWithString: "AI服务：未知")
    var commandOutput = NSTextView()
    var testOutput = NSTextView()
    var logOutput = NSTextView()

    var providerPopup = NSPopUpButton()
    var baseURLField = NSTextField(string: "https://api.deepseek.com/v1")
    var apiKeyField = NSSecureTextField(string: "")
    var modelPopup = NSPopUpButton()
    var modelField = NSTextField(string: "deepseek-chat")
    var tempField = NSTextField(string: "0.3")
    var maxTokensField = NSTextField(string: "600")
    var timeoutField = NSTextField(string: "30")
    var replyPrefixField = NSTextField(string: "AI：")
    var keywordsField = NSTextField(string: "")
    var onebotAPIField = NSTextField(string: "http://127.0.0.1:58080")
    var cooldownField = NSTextField(string: "2")
    var maxReplyCharsField = NSTextField(string: "600")
    var requireKeywordCheck = NSButton(checkboxWithTitle: "必须包含关键词才回复", target: nil, action: nil)
    var dryRunCheck = NSButton(checkboxWithTitle: "Dry Run：只记录不发送", target: nil, action: nil)
    var ignoreSelfCheck = NSButton(checkboxWithTitle: "忽略第二微信自己发出的消息", target: nil, action: nil)
    var systemPromptView = NSTextView()

    var groupNameField = NSTextField(string: "值班群")
    var groupIDField = NSTextField(string: "")
    var groupsView = NSTextView()
    var recentView = NSTextView()
    var targetGroups: [TargetGroup] = []

    var aiTestPromptField = NSTextField(string: "请回复：AI接口测试成功")
    var callbackTextField = NSTextField(string: "值班群AI回调测试")
    var sendTextField = NSTextField(string: "第二微信助手固定消息测试")

    let providers: [(String, String, String)] = [
        ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
        ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
        ("第三方中转站(OpenAI兼容)", "https://你的中转站域名/v1", ""),
        ("OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
        ("本地OpenAI兼容", "http://127.0.0.1:1234/v1", "local-model")
    ]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        appendCommand("[\(timeString())] 界面已加载。首次使用请点“加载配置/授权”或“刷新状态”；如果系统询问访问“文稿/Documents”，请点“允许”。\n")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1120, height: 780), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "第二微信 AI 助手管理器"
        window.minSize = NSSize(width: 980, height: 680)
        let content = NSView()
        window.contentView = content
        tabView.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(tabView)
        NSLayoutConstraint.activate([
            tabView.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            tabView.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            tabView.topAnchor.constraint(equalTo: content.topAnchor, constant: 12),
            tabView.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -12)
        ])
        addTab("总控/后台", buildDashboard())
        addTab("AI 配置", buildAI())
        addTab("群配置", buildGroups())
        addTab("测试", buildTests())
        addTab("日志", buildLogs())
    }

    func addTab(_ title: String, _ view: NSView) {
        let item = NSTabViewItem(identifier: title)
        item.label = title
        item.view = view
        tabView.addTabViewItem(item)
    }

    func baseStack() -> NSStackView {
        let s = NSStackView()
        s.orientation = .vertical
        s.alignment = .width
        s.distribution = .fill
        s.spacing = 10
        s.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        s.translatesAutoresizingMaskIntoConstraints = false
        return s
    }

    func row(_ views: [NSView]) -> NSStackView {
        let s = NSStackView(views: views)
        s.orientation = .horizontal
        s.alignment = .centerY
        s.distribution = .fill
        s.spacing = 8
        return s
    }

    func label(_ text: String, width: CGFloat = 110) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.textColor = .labelColor
        l.widthAnchor.constraint(equalToConstant: width).isActive = true
        l.alignment = .right
        return l
    }

    func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        return b
    }

    func titledBox(_ title: String, _ inner: NSView) -> NSBox {
        let box = NSBox()
        box.title = title
        box.boxType = .primary
        box.contentViewMargins = NSSize(width: 10, height: 10)
        inner.translatesAutoresizingMaskIntoConstraints = false
        box.contentView?.addSubview(inner)
        if let cv = box.contentView {
            NSLayoutConstraint.activate([
                inner.leadingAnchor.constraint(equalTo: cv.leadingAnchor),
                inner.trailingAnchor.constraint(equalTo: cv.trailingAnchor),
                inner.topAnchor.constraint(equalTo: cv.topAnchor),
                inner.bottomAnchor.constraint(equalTo: cv.bottomAnchor)
            ])
        }
        return box
    }

    func scrollText(_ tv: NSTextView, height: CGFloat = 180, editable: Bool = true) -> NSScrollView {
        tv.isEditable = editable
        tv.isRichText = false
        tv.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        tv.textColor = .labelColor
        tv.backgroundColor = .textBackgroundColor
        let sv = NSScrollView()
        sv.hasVerticalScroller = true
        sv.hasHorizontalScroller = true
        sv.borderType = .bezelBorder
        sv.documentView = tv
        sv.heightAnchor.constraint(greaterThanOrEqualToConstant: height).isActive = true
        return sv
    }

    func buildDashboard() -> NSView {
        let v = NSView(); let s = baseStack(); v.addSubview(s)
        NSLayoutConstraint.activate([s.leadingAnchor.constraint(equalTo: v.leadingAnchor), s.trailingAnchor.constraint(equalTo: v.trailingAnchor), s.topAnchor.constraint(equalTo: v.topAnchor), s.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        statusWechat.font = .boldSystemFont(ofSize: 14); statusOneBot.font = .boldSystemFont(ofSize: 14); statusAI.font = .boldSystemFont(ofSize: 14)
        s.addArrangedSubview(titledBox("当前状态", row([statusWechat, statusOneBot, statusAI])))
        let actions = NSStackView(); actions.orientation = .vertical; actions.spacing = 8
        actions.addArrangedSubview(row([button("加载配置/授权", #selector(loadConfigAction(_:))), button("保存配置", #selector(saveConfigAction(_:))), button("启动第二微信", #selector(startWeChat(_:))), button("启动 OneBot", #selector(startOneBot(_:))), button("启动/复用 AI 服务", #selector(startAI(_:))), button("一键启动全部", #selector(startAll(_:)))]))
        actions.addArrangedSubview(row([button("停止 AI 服务", #selector(stopAI(_:))), button("停止 OneBot", #selector(stopOneBot(_:))), button("停止后台(AI+OneBot)", #selector(stopBackend(_:))), button("刷新状态", #selector(refreshStatus(_:))), button("打开日志目录", #selector(openLogDir(_:)))]))
        s.addArrangedSubview(titledBox("启动 / 管理后台", actions))
        let note = NSTextField(labelWithString: "第二微信：\(NSHomeDirectory())/Applications/WeChat2.app\n所有脚本只识别 com.tencent.xinWeChat.instance2 和 WeChat2.app；停止后台只停止 AI/OneBot，不关闭主微信，也不关闭第二微信。")
        note.lineBreakMode = .byWordWrapping
        s.addArrangedSubview(titledBox("固定约束", note))
        let outBox = titledBox("命令输出", scrollText(commandOutput, height: 300, editable: false))
        s.addArrangedSubview(outBox)
        outBox.setContentHuggingPriority(.defaultLow, for: .vertical)
        return v
    }

    func buildAI() -> NSView {
        let v = NSView(); let s = baseStack(); v.addSubview(s)
        NSLayoutConstraint.activate([s.leadingAnchor.constraint(equalTo: v.leadingAnchor), s.trailingAnchor.constraint(equalTo: v.trailingAnchor), s.topAnchor.constraint(equalTo: v.topAnchor), s.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        providerPopup.addItems(withTitles: providers.map{$0.0}); providerPopup.target = self; providerPopup.action = #selector(providerChanged(_:))
        modelPopup.addItems(withTitles: ["deepseek-chat", "gpt-4o-mini"]); modelPopup.target = self; modelPopup.action = #selector(modelPopupChanged(_:))
        baseURLField.widthAnchor.constraint(greaterThanOrEqualToConstant: 520).isActive = true
        apiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 520).isActive = true
        modelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true
        s.addArrangedSubview(row([label("供应商预设"), providerPopup]))
        s.addArrangedSubview(row([label("Base URL"), baseURLField]))
        s.addArrangedSubview(row([label("API Key"), apiKeyField]))
        s.addArrangedSubview(row([label("模型"), modelField, modelPopup, button("获取模型列表", #selector(fetchModels(_:)))]))
        s.addArrangedSubview(row([label("temperature"), tempField, label("max_tokens"), maxTokensField, label("timeout秒"), timeoutField]))
        s.addArrangedSubview(row([label("回复前缀"), replyPrefixField, label("OneBot API"), onebotAPIField]))
        s.addArrangedSubview(row([label("触发关键词"), keywordsField]))
        s.addArrangedSubview(row([requireKeywordCheck, dryRunCheck, ignoreSelfCheck]))
        s.addArrangedSubview(row([label("群冷却秒"), cooldownField, label("最大回复字数"), maxReplyCharsField]))
        systemPromptView.string = "你是微信群值班助手。只根据群聊最新消息和少量上下文，用中文简洁回复；不确定时先澄清；不要编造事实；不要输出多余客套；回复适合直接发到群里。"
        s.addArrangedSubview(titledBox("System Prompt", scrollText(systemPromptView, height: 150, editable: true)))
        s.addArrangedSubview(row([button("保存配置", #selector(saveConfigAction(_:))), button("保存并重启 AI", #selector(saveRestartAI(_:))), button("测试 AI 接口", #selector(testAI(_:)))]))
        return v
    }

    func buildGroups() -> NSView {
        let v = NSView(); let s = baseStack(); v.addSubview(s)
        NSLayoutConstraint.activate([s.leadingAnchor.constraint(equalTo: v.leadingAnchor), s.trailingAnchor.constraint(equalTo: v.trailingAnchor), s.topAnchor.constraint(equalTo: v.topAnchor), s.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        groupIDField.widthAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true
        groupsView.string = ""
        s.addArrangedSubview(row([label("群名"), groupNameField, label("group_id"), groupIDField, button("添加/更新", #selector(addUpdateGroup(_:))), button("删除此ID", #selector(deleteGroup(_:))), button("保存群配置", #selector(saveConfigAction(_:)))]))
        let grids = NSStackView(); grids.orientation = .horizontal; grids.alignment = .height; grids.distribution = .fillEqually; grids.spacing = 12
        grids.addArrangedSubview(titledBox("AI 会回复的群", scrollText(groupsView, height: 360, editable: false)))
        grids.addArrangedSubview(titledBox("最近真实群消息", scrollText(recentView, height: 360, editable: false)))
        s.addArrangedSubview(grids)
        s.addArrangedSubview(row([button("刷新最近群ID", #selector(refreshRecentGroups(_:))), button("把左侧输入框保存为值班群", #selector(addUpdateGroup(_:)))]))
        let hint = NSTextField(labelWithString: "不知道值班群 ID 时：先在值班群发一句话，再点“刷新最近群ID”，把对应 group_id 复制到输入框保存。")
        s.addArrangedSubview(hint)
        return v
    }

    func buildTests() -> NSView {
        let v = NSView(); let s = baseStack(); v.addSubview(s)
        NSLayoutConstraint.activate([s.leadingAnchor.constraint(equalTo: v.leadingAnchor), s.trailingAnchor.constraint(equalTo: v.trailingAnchor), s.topAnchor.constraint(equalTo: v.topAnchor), s.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        aiTestPromptField.widthAnchor.constraint(greaterThanOrEqualToConstant: 520).isActive = true
        callbackTextField.widthAnchor.constraint(greaterThanOrEqualToConstant: 520).isActive = true
        sendTextField.widthAnchor.constraint(greaterThanOrEqualToConstant: 520).isActive = true
        s.addArrangedSubview(row([label("AI测试提示词"), aiTestPromptField, button("测试 AI 接口(不发微信)", #selector(testAI(_:)))]))
        s.addArrangedSubview(row([label("回调测试文本"), callbackTextField, button("测试 AI 回调链路", #selector(testCallback(_:)))]))
        s.addArrangedSubview(row([label("固定发送文本"), sendTextField, button("测试 OneBot 发送", #selector(testOneBotSend(_:)))]))
        let info = NSTextField(labelWithString: "AI接口测试只请求模型接口，不发送微信。AI回调链路会模拟值班群消息；如果AI服务已配置Key且 dry_run 关闭，会由第二微信发到目标群。OneBot发送测试会直接发送固定文本到目标群。")
        info.lineBreakMode = .byWordWrapping
        s.addArrangedSubview(info)
        s.addArrangedSubview(titledBox("测试输出", scrollText(testOutput, height: 420, editable: false)))
        return v
    }

    func buildLogs() -> NSView {
        let v = NSView(); let s = baseStack(); v.addSubview(s)
        NSLayoutConstraint.activate([s.leadingAnchor.constraint(equalTo: v.leadingAnchor), s.trailingAnchor.constraint(equalTo: v.trailingAnchor), s.topAnchor.constraint(equalTo: v.topAnchor), s.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        s.addArrangedSubview(row([button("刷新 AI 日志", #selector(loadAILog(_:))), button("刷新 OneBot 日志", #selector(loadOneBotLog(_:))), button("打开日志目录", #selector(openLogDir(_:)))]))
        s.addArrangedSubview(scrollText(logOutput, height: 600, editable: false))
        return v
    }

    func append(_ tv: NSTextView, _ text: String) {
        DispatchQueue.main.async {
            tv.string += text
            tv.scrollToEndOfDocument(nil)
        }
    }
    func appendCommand(_ text: String) { append(commandOutput, text) }
    func appendTest(_ text: String) { append(testOutput, text) }

    func script(_ name: String) -> String { ROOT_DIR + "/scripts/" + name }

    func runCommand(_ title: String, _ args: [String], output: NSTextView? = nil, refresh: Bool = true) {
        let tv = output ?? commandOutput
        append(tv, "\n[\(timeString())] \(title)\n$ \(args.joined(separator: " "))\n")
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process(); p.currentDirectoryURL = URL(fileURLWithPath: ROOT_DIR); p.executableURL = URL(fileURLWithPath: args[0]); p.arguments = Array(args.dropFirst())
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { h in
                let data = h.availableData
                if data.count > 0, let s = String(data: data, encoding: .utf8) { self.append(tv, s) }
            }
            do { try p.run(); p.waitUntilExit() } catch { self.append(tv, "执行失败：\(error)\n") }
            pipe.fileHandleForReading.readabilityHandler = nil
            self.append(tv, "[\(self.timeString())] 退出码：\(p.terminationStatus)\n")
            if refresh { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.refreshStatus(nil) } }
        }
    }

    func timeString() -> String { let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f.string(from: Date()) }

    @objc func providerChanged(_ sender: Any?) {
        let idx = providerPopup.indexOfSelectedItem
        if idx >= 0 && idx < providers.count { baseURLField.stringValue = providers[idx].1; if !providers[idx].2.isEmpty { modelField.stringValue = providers[idx].2 } }
    }
    @objc func modelPopupChanged(_ sender: Any?) { if let title = modelPopup.selectedItem?.title { modelField.stringValue = title } }

    func loadConfig() {
        let env = parseEnv(ENV_PATH)
        if let data = try? Data(contentsOf: URL(fileURLWithPath: CONFIG_PATH)), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let api = obj["onebot_api"] as? String { onebotAPIField.stringValue = api }
            if let rp = obj["reply_prefix"] as? String { replyPrefixField.stringValue = rp }
            if let arr = obj["trigger_keywords"] as? [String] { keywordsField.stringValue = arr.joined(separator: "，") }
            if let v = obj["require_keyword"] as? Bool { requireKeywordCheck.state = v ? .on : .off }
            if let v = obj["dry_run"] as? Bool { dryRunCheck.state = v ? .on : .off }
            if let v = obj["ignore_self_messages"] as? Bool { ignoreSelfCheck.state = v ? .on : .off }
            if let v = obj["min_seconds_between_replies_per_group"] { cooldownField.stringValue = "\(v)" }
            if let v = obj["max_reply_chars"] { maxReplyCharsField.stringValue = "\(v)" }
            if let ai = obj["ai"] as? [String: Any] {
                if let b = ai["base_url"] as? String { baseURLField.stringValue = b }
                if let m = ai["model"] as? String { modelField.stringValue = m }
                if let t = ai["temperature"] { tempField.stringValue = "\(t)" }
                if let t = ai["max_tokens"] { maxTokensField.stringValue = "\(t)" }
                if let t = ai["timeout_seconds"] { timeoutField.stringValue = "\(t)" }
                if let p = ai["system_prompt"] as? String { systemPromptView.string = p }
            }
            targetGroups.removeAll()
            if let groups = obj["target_groups"] as? [[String: Any]] {
                for g in groups { if let id = g["id"] as? String { targetGroups.append(TargetGroup(name: (g["name"] as? String) ?? id, id: id)) } }
            }
            renderGroups()
        }
        if let key = env["AI_REPLY_API_KEY"] ?? env["OPENAI_API_KEY"] { apiKeyField.stringValue = key }
        if let b = env["AI_REPLY_BASE_URL"] { baseURLField.stringValue = b }
        if let m = env["AI_REPLY_MODEL"] { modelField.stringValue = m }
        inferProvider()
        if let first = targetGroups.first { groupNameField.stringValue = first.name; groupIDField.stringValue = first.id }
    }

    func inferProvider() {
        let b = baseURLField.stringValue.lowercased()
        let idx: Int
        if b.contains("deepseek") { idx = 0 } else if b.contains("api.openai.com") { idx = 1 } else if b.contains("openrouter") { idx = 3 } else if b.contains("127.0.0.1") || b.contains("localhost") { idx = 4 } else { idx = 2 }
        providerPopup.selectItem(at: idx)
    }

    @discardableResult
    func saveConfig(showAlert: Bool = true) -> Bool {
        var raw: [String: Any] = [:]
        if let data = try? Data(contentsOf: URL(fileURLWithPath: CONFIG_PATH)), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { raw = obj }
        raw["enabled"] = true
        raw["listen_host"] = raw["listen_host"] ?? "127.0.0.1"
        raw["listen_port"] = raw["listen_port"] ?? 36060
        raw["onebot_api"] = onebotAPIField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        raw["target_groups"] = targetGroups.map { ["name": $0.name, "id": $0.id] }
        raw["reply_prefix"] = replyPrefixField.stringValue
        raw["ignore_prefixes"] = raw["ignore_prefixes"] ?? ["AI：", "AI:", "🤖"]
        raw["trigger_keywords"] = keywordsField.stringValue.replacingOccurrences(of: "，", with: ",").split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        raw["require_keyword"] = requireKeywordCheck.state == .on
        raw["dry_run"] = dryRunCheck.state == .on
        raw["ignore_self_messages"] = ignoreSelfCheck.state == .on
        raw["allowed_user_ids"] = raw["allowed_user_ids"] ?? []
        raw["ignored_user_ids"] = raw["ignored_user_ids"] ?? []
        raw["min_seconds_between_replies_per_group"] = Double(cooldownField.stringValue) ?? 2
        raw["max_reply_chars"] = Int(maxReplyCharsField.stringValue) ?? 600
        raw["max_context_messages"] = raw["max_context_messages"] ?? 8
        raw["send_delay_seconds"] = raw["send_delay_seconds"] ?? 0.2
        raw["ai"] = [
            "provider": "openai_compatible",
            "base_url": baseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/")),
            "api_key_env": "OPENAI_API_KEY",
            "model": modelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            "temperature": Double(tempField.stringValue) ?? 0.3,
            "max_tokens": Int(maxTokensField.stringValue) ?? 600,
            "timeout_seconds": Int(timeoutField.stringValue) ?? 30,
            "system_prompt": systemPromptView.string
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: raw, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: URL(fileURLWithPath: CONFIG_PATH))
            let env = """
            # 由 第二微信 AI 助手管理器 生成。
            export AI_REPLY_PROVIDER='openai_compatible'
            export AI_REPLY_API_KEY='\(apiKeyField.stringValue.replacingOccurrences(of: "'", with: "'\\''"))'
            export AI_REPLY_BASE_URL='\(baseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))'
            export AI_REPLY_MODEL='\(modelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))'
            export AI_REPLY_TEMPERATURE='\(tempField.stringValue)'
            export AI_REPLY_MAX_TOKENS='\(maxTokensField.stringValue)'
            export AI_REPLY_TIMEOUT_SECONDS='\(timeoutField.stringValue)'
            export AI_REPLY_ONEBOT_API='\(onebotAPIField.stringValue)'
            export AI_REPLY_DRY_RUN='\(dryRunCheck.state == .on ? "1" : "0")'
            export AI_REPLY_REQUIRE_KEYWORD='\(requireKeywordCheck.state == .on ? "1" : "0")'
            export AI_REPLY_IGNORE_SELF='\(ignoreSelfCheck.state == .on ? "1" : "0")'
            """
            try env.write(toFile: ENV_PATH, atomically: true, encoding: .utf8)
            appendCommand("[\(timeString())] 配置已保存\n")
            if showAlert { alert("已保存", "配置已保存。") }
            return true
        } catch { alert("保存失败", "\(error)"); return false }
    }

    func renderGroups() {
        groupsView.string = targetGroups.map { "\($0.name)\t\($0.id)" }.joined(separator: "\n")
    }

    func parseEnv(_ path: String) -> [String: String] {
        guard let txt = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
        var r: [String: String] = [:]
        for raw in txt.split(separator: "\n", omittingEmptySubsequences: false) {
            var line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("export ") { line = String(line.dropFirst(7)) }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let k = String(line[..<eq]).trimmingCharacters(in: .whitespacesAndNewlines)
            var v = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespacesAndNewlines)
            if (v.hasPrefix("'") && v.hasSuffix("'")) || (v.hasPrefix("\"") && v.hasSuffix("\"")) { v = String(v.dropFirst().dropLast()) }
            r[k] = v
        }
        return r
    }

    func alert(_ title: String, _ msg: String) { let a = NSAlert(); a.messageText = title; a.informativeText = msg; a.runModal() }

    @objc func loadConfigAction(_ s: Any?) { loadConfig(); refreshStatus(nil) }
    @objc func saveConfigAction(_ s: Any?) { _ = saveConfig(showAlert: true) }
    @objc func saveRestartAI(_ s: Any?) { if saveConfig(showAlert: false) { runCommand("保存并重启 AI", ["/bin/bash", "-lc", "\(script("stop_ai_reply.sh")); \(script("start_ai_reply.sh"))"]) } }
    @objc func startWeChat(_ s: Any?) { runCommand("启动第二微信", [script("launch_wechat2_4_1_11_53.sh")]) }
    @objc func startOneBot(_ s: Any?) { runCommand("启动 OneBot", [script("start_onebot_wechat2.sh")]) }
    @objc func startAI(_ s: Any?) { _ = saveConfig(showAlert: false); runCommand("启动/复用 AI 服务", [script("start_ai_reply.sh")]) }
    @objc func startAll(_ s: Any?) { _ = saveConfig(showAlert: false); runCommand("一键启动全部", [script("run_wechat2_ai_reply.sh")]) }
    @objc func stopAI(_ s: Any?) { runCommand("停止 AI 服务", [script("stop_ai_reply.sh")]) }
    @objc func stopOneBot(_ s: Any?) { runCommand("停止 OneBot", [script("stop_onebot_wechat2.sh")]) }
    @objc func stopBackend(_ s: Any?) { runCommand("停止后台", [script("stop_backend_wechat2.sh")]) }
    @objc func openLogDir(_ s: Any?) { NSWorkspace.shared.open(URL(fileURLWithPath: LOG_DIR)) }

    @objc func refreshStatus(_ s: Any?) {
        DispatchQueue.global().async {
            let out1 = self.capture([self.script("status_wechat2_onebot.sh")])
            let out2 = self.capture([self.script("status_ai_reply.sh")])
            let wp = self.match(out1, "WeChat2 PID=([0-9]+)") ?? ""
            let op = self.match(out1, "OneBot PID=([0-9]+)") ?? ""
            let ap = self.match(out2, "AI reply PID=([0-9]+)") ?? ""
            DispatchQueue.main.async {
                self.statusWechat.stringValue = wp.isEmpty ? "WeChat2：未运行" : "WeChat2：运行 PID=\(wp)"
                self.statusOneBot.stringValue = op.isEmpty ? "OneBot：未运行" : "OneBot：运行 PID=\(op)"
                self.statusAI.stringValue = ap.isEmpty ? "AI服务：未运行" : "AI服务：运行 PID=\(ap)"
                self.commandOutput.string = (out1 + "\n--- AI ---\n" + out2).suffix(20000).description
            }
        }
    }

    @objc func addUpdateGroup(_ s: Any?) {
        let gid = groupIDField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if gid.isEmpty { alert("缺少 group_id", "请填写 group_id"); return }
        let name = groupNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? gid : groupNameField.stringValue
        if let i = targetGroups.firstIndex(where: {$0.id == gid}) { targetGroups[i] = TargetGroup(name: name, id: gid) } else { targetGroups.append(TargetGroup(name: name, id: gid)) }
        renderGroups()
    }
    @objc func deleteGroup(_ s: Any?) { let gid = groupIDField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines); targetGroups.removeAll{$0.id == gid}; renderGroups() }
    @objc func refreshRecentGroups(_ s: Any?) { runCommand("刷新最近群ID", [script("recent_group_ids.sh"), "30"], output: recentView, refresh: false) }

    @objc func fetchModels(_ s: Any?) {
        let base = baseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + "/models") else { alert("URL错误", base); return }
        appendTest("[\(timeString())] GET \(url.absoluteString)\n")
        var req = URLRequest(url: url); req.httpMethod = "GET"; req.setValue("application/json", forHTTPHeaderField: "Accept")
        if !apiKeyField.stringValue.isEmpty { req.setValue("Bearer \(apiKeyField.stringValue)", forHTTPHeaderField: "Authorization") }
        URLSession.shared.dataTask(with: req) { data, resp, err in
            if let err = err { self.appendTest("获取模型失败：\(err)\n"); return }
            guard let data = data else { self.appendTest("获取模型失败：空响应\n"); return }
            var models: [String] = []
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let arr = obj["data"] as? [Any] {
                for item in arr { if let d = item as? [String: Any], let id = d["id"] as? String { models.append(id) } else if let s = item as? String { models.append(s) } }
            }
            DispatchQueue.main.async {
                if models.isEmpty { self.appendTest("未解析到模型：\n\(String(data: data, encoding: .utf8) ?? "")\n") }
                else { self.modelPopup.removeAllItems(); self.modelPopup.addItems(withTitles: models); self.modelField.stringValue = models[0]; self.appendTest("获取到模型：\n\(models.joined(separator: "\n"))\n") }
            }
        }.resume()
    }

    @objc func testAI(_ s: Any?) {
        let base = baseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + "/chat/completions") else { alert("URL错误", base); return }
        let payload: [String: Any] = ["model": modelField.stringValue, "messages": [["role":"system", "content": systemPromptView.string], ["role":"user", "content": aiTestPromptField.stringValue]], "temperature": Double(tempField.stringValue) ?? 0.3, "max_tokens": Int(maxTokensField.stringValue) ?? 600]
        appendTest("[\(timeString())] POST \(url.absoluteString) model=\(modelField.stringValue)\n")
        postJSON(url: url, payload: payload) { text in self.appendTest("AI接口测试返回：\n\(text)\n") }
    }

    @objc func testCallback(_ s: Any?) {
        guard let g = targetGroups.first else { alert("没有目标群", "请先保存值班群 group_id"); return }
        _ = saveConfig(showAlert: false)
        runCommand("测试 AI 回调链路", [script("test_ai_reply_event.sh"), g.id, callbackTextField.stringValue], output: testOutput)
    }
    @objc func testOneBotSend(_ s: Any?) {
        guard let g = targetGroups.first else { alert("没有目标群", "请先保存值班群 group_id"); return }
        let base = onebotAPIField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + "/send_group_msg") else { return }
        let payload: [String: Any] = ["group_id": g.id, "message": [["type":"text", "data": ["text": sendTextField.stringValue]]]]
        postJSON(url: url, payload: payload) { text in self.appendTest("OneBot发送返回：\n\(text)\n") }
    }

    func postJSON(url: URL, payload: [String: Any], done: @escaping (String)->Void) {
        var req = URLRequest(url: url); req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !apiKeyField.stringValue.isEmpty && url.absoluteString.contains("/chat/completions") { req.setValue("Bearer \(apiKeyField.stringValue)", forHTTPHeaderField: "Authorization") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: req) { data, resp, err in
            if let err = err { done("失败：\(err)"); return }
            guard let data = data else { done("空响应"); return }
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let choices = obj["choices"] as? [[String: Any]], let msg = choices.first?["message"] as? [String: Any], let content = msg["content"] as? String { done(content); return }
            done(String(data: data, encoding: .utf8) ?? "无法解码响应")
        }.resume()
    }

    @objc func loadAILog(_ s: Any?) { loadLog(LOG_DIR + "/ai-reply.log") }
    @objc func loadOneBotLog(_ s: Any?) { loadLog(LOG_DIR + "/onebot-wechat2.log") }
    func loadLog(_ path: String) { logOutput.string = (try? String(contentsOfFile: path, encoding: .utf8).suffix(100000).description) ?? "日志不存在：\(path)" }

    func capture(_ args: [String]) -> String {
        let p = Process(); p.currentDirectoryURL = URL(fileURLWithPath: ROOT_DIR); p.executableURL = URL(fileURLWithPath: args[0]); p.arguments = Array(args.dropFirst())
        let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
        do { try p.run(); p.waitUntilExit(); let d = pipe.fileHandleForReading.readDataToEndOfFile(); return String(data: d, encoding: .utf8) ?? "" } catch { return "执行失败：\(error)\n" }
    }
    func match(_ text: String, _ pattern: String) -> String? { guard let r = try? NSRegularExpression(pattern: pattern), let m = r.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)), m.numberOfRanges > 1, let range = Range(m.range(at: 1), in: text) else { return nil }; return String(text[range]) }
}


let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
