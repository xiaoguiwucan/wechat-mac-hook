# 项目规则

## 1. 第二微信边界

- 只允许操作第二微信：默认 `~/Applications/WeChat2.app`。
- 不允许修改、重签、注入或附加主微信：`/Applications/WeChat.app`。
- 所有脚本必须显式校验目标路径、Bundle ID 或进程路径，避免误伤主微信。
- 停止后台服务时只能停止本项目启动的 OneBot / AI / Web 进程，不关闭主微信。

## 2. 数据隔离规则

- 第二微信数据默认写入：`~/Library/Application Support/WeChatSecond`。
- Hook 只在第二实例环境变量存在时启用。
- 新增文件访问逻辑时，必须确认不会写入主微信容器、主微信 Group Container 或主微信偏好设置。

## 3. 配置规则

- 真实配置文件不提交：
  - `config/ai_reply.env`
  - `config/ai_reply_config.json`
- 新增配置项必须同步更新示例文件：
  - `config/ai_reply.env.example`
  - `config/ai_reply_config.example.json`
- API Key 只允许通过环境变量读取，不允许硬编码到源码或 README。

## 4. 群聊回复规则

- AI 回复必须经过群白名单判断。
- 未授权群只允许记录日志，不调用模型、不发送回复。
- 回复前缀允许为空字符串，保存时不得自动恢复默认前缀。
- 机器人性格必须进入系统提示词，并优先约束回复风格。

## 5. 模型渠道规则

- 渠道配置必须支持 OpenAI-compatible API。
- 新增渠道后应立即进行健康检测并更新状态灯。
- 发送失败时只在启用渠道中切换，失败渠道进入冷却期。
- 删除渠道时不得删除其他渠道的 API Key。

## 6. Web 后台规则

- 后台按钮必须连接真实 API，不使用假状态或模拟成功。
- UI 要适配桌面和窄屏，避免横向溢出。
- 修改配置时使用 revision 校验，避免旧页面覆盖新配置。
- 实时日志只展示本项目运行日志，不展示敏感密钥。

## 7. 提交规则

- 提交前执行基础检查：

```bash
python3 -m py_compile web_admin/server.py ai_reply/ai_reply_server.py
node --check web_admin/static/app.js
```

- 提交前确认没有敏感信息：

```bash
git status --short
git diff --cached --name-only
```

- 不提交以下内容：
  - 微信安装包、App 副本、DMG。
  - dylib、OneBot 二进制、构建产物。
  - 日志、截图、缓存图片、真实群 ID、真实 wxid。
  - API Key、Token、Cookie、私有证书。

## 8. 版本规则

- 公开版本使用 `VMAJOR.MINOR.PATCH`。
- 每次发布必须更新 `CHANGELOG.md`。
- 首个公开版本为 `V0.0.1`。

