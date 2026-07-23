# 项目规则

## 1. 唯一微信边界

- 运行目标固定为 `/Applications/WeChat.app`。
- Bundle ID 必须为 `com.tencent.xinWeChat`，当前适配版本必须为 `4.1.11.53`、build `269109`。
- SIP 开启时仅允许按 `config/wechat_target.json` 锁定摘要，把 Frida Gadget 注入并本地重签当前唯一 App；修改前必须保留不可运行的压缩回滚备份。
- 禁止复制、改 Bundle ID、使用 `-n`、`--allow_multi_open`、`--multi_open` 或创建第二实例。
- 启动和附加前必须校验目标可执行文件的真实路径；检测到其他 WeChat 主进程路径时立即停止。
- 停止后台服务时只停止本项目启动的 OneBot / AI / Web 进程，不关闭微信。

## 2. 项目数据规则

- 项目自身状态写入 `~/Library/Application Support/WeChatAgent`。
- 微信账号数据直接使用官方容器 `~/Library/Containers/com.tencent.xinWeChat`，项目不得重定向 HOME、容器、偏好设置或缓存路径。
- 版本地址配置只允许与已校验的当前微信版本配套使用。

## 3. 配置规则

- 真实配置文件不提交：`config/ai_reply.env`、`config/ai_reply_config.json`。
- 新增配置项必须同步更新示例文件。
- API Key 只允许通过环境变量读取，不允许硬编码到源码或文档。

## 4. 群聊回复规则

- AI 回复必须经过群白名单判断。
- 未授权群只记录日志，不调用模型、不发送回复。
- 回复前缀允许为空，保存时不得自动恢复默认前缀。
- 机器人性格必须进入系统提示词，并优先约束回复风格。

## 5. Web 后台规则

- 后台按钮必须连接真实 API，不使用假状态或模拟成功。
- 配置写入使用 revision 校验和原子替换。
- 实时日志只展示本项目日志，不展示密钥。

## 6. 提交前检查

```bash
python3 -m py_compile web_admin/server.py ai_reply/ai_reply_server.py
node --check web_admin/static/app.js
python3 -m unittest discover -s tests -p 'test_*.py'
rg -n 'WeChat2|instance2|allow_multi_open|multi_open|第二微信|多开' --glob '!CHANGELOG.md' --glob '!README.md' --glob '!RULES.md'
```

不提交微信安装包、App 副本、DMG、二进制构建产物、日志、真实群 ID、真实 wxid、API Key、Token、Cookie 或私有证书。
