# CHANGELOG

本项目遵循 `VMAJOR.MINOR.PATCH` 版本号。当前首个公开版本为 `V0.0.1`。

## V0.0.1 - 2026-07-12

### Added

- 初始化 `wechat-mac-hook` 项目。
- 增加 macOS 第二微信隔离 Hook：
  - 拦截 `NSHomeDirectory()`、`NSSearchPathForDirectoriesInDomains(...)`、`getenv("HOME")`。
  - 拦截常见 C 文件 API，重定向第二微信相关路径。
  - 增加环境变量保护，仅在第二实例环境中生效。
- 增加第二微信安装、启动、状态检测脚本。
- 增加微信 macOS `4.1.11.53` OneBot 运行脚本与地址配置。
- 增加 OneBot -> AI -> OneBot 群聊回复桥接服务。
- 增加 Web 管理后台：
  - 运行总览。
  - 模型配置。
  - 群聊策略。
  - 测试中心。
  - 完整实时日志。
- 增加多模型渠道能力：
  - 渠道新增、保存、删除。
  - 模型列表获取。
  - 手动测试与自动健康检测。
  - 失败冷却与自动切换。
- 增加群权限白名单：只有勾选群才会触发 AI 回复。
- 增加机器人性格编辑器，并注入系统提示词。
- 增加示例配置：
  - `config/ai_reply_config.example.json`
  - `config/ai_reply.env.example`
- 增加项目规则文档 `RULES.md`。

### Changed

- 将 README 重写为公开仓库说明文档。
- 将桌面端 Swift 管理器的项目根目录改为环境变量 / 当前目录 / 源码路径自动推断，移除本机绝对路径。
- 将测试脚本默认群 ID 改为示例值，避免提交真实群信息。

### Security

- `.gitignore` 默认排除真实密钥、真实配置、日志、下载包、构建产物、App 副本和运行缓存。
- 默认不提交 `config/ai_reply.env` 与 `config/ai_reply_config.json`。

