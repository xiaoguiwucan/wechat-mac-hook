# 高速记忆与 Hermes 自动化

## 运行结构

- 唯一官方微信和现有 Hook 仍是唯一收发入口。
- SQLite 在 OneBot 回调内事务保存消息和 `durable_outbox`，回复线程不等待中央服务。
- `durable_sync.py` 把 outbox 幂等同步到 PostgreSQL，并按 SHA-256 把媒体归档到 MinIO。
- PostgreSQL 使用 PGroonga 做中文全文检索，预装 pgvector/HNSW 供云端向量任务使用。
- `graphiti_bridge.py` 在独立线程把 PostgreSQL 事件投影到 Graphiti + FalkorDB。
- `hermes_automation.py` 只通过 Hermes API Server 异步执行运维任务；没有启用 Hermes 微信通道。

## 快速链路

回复前的所有检索共享 250ms 截止时间。SQLite 最近消息、人物/群缓存、PGroonga
和 Graphiti 并行执行；到期只使用已完成结果。Embedding、OCR、Graphiti、Hermes
均不在实时回复线程里等待。

当前机器不启动 8B 本地 Embedding/Reranker。默认启用 `local_hash` 字符 n-gram
向量，零模型内存完成后台回填和本地召回；向量同时截断为 4000 维写入 PostgreSQL
halfvec/HNSW。它与中文全文、人物缓存和 Graphiti 并行，不阻塞实时回复。

## 基础设施

```bash
cp infrastructure/.env.example infrastructure/.env
docker compose --env-file infrastructure/.env \
  -f infrastructure/docker-compose.yml up -d --build
```

端口：

- PostgreSQL：`127.0.0.1:5432`
- MinIO API/Console：`127.0.0.1:9000/9001`
- FalkorDB/Browser：`127.0.0.1:6379/3101`
- Hermes API Server：`127.0.0.1:8642`

## 历史、迁移与备份

```bash
# 只读导入微信 4.x 解密快照
PYTHONPATH=tools/runtime/python python3 scripts/import_wechat4_history.py

# 把旧 SQLite 消息补入 outbox
PYTHONPATH=tools/runtime/python python3 scripts/migrate_sqlite_to_postgres.py

# 备份 SQLite、PostgreSQL、MinIO 清单和全部媒体原件
./scripts/backup_durable_memory.sh

# 校验数据库、dump、清单和每一个媒体对象
./scripts/verify_memory_backup.sh backups/备份目录

# 可选：安装 AI 与后台的登录自启/崩溃拉起守护
./scripts/install_launch_agents.sh
```

守护安装包含健康检查和失败回滚。若 macOS 拒绝 LaunchAgent 读取 Documents，
脚本会撤销安装，避免产生重启风暴。

导入器只读打开快照，不接触运行中微信数据库；重复执行依靠稳定 `event_id`
去重。PostgreSQL 还使用账号、群、方向、消息 ID、时间的唯一约束消除实时记录
与历史快照的重复。

## Hermes 权限

- `read`：群成员可以查询状态。
- `write`：必须是后台配置的群管理员。
- `high`：删除、强推、密钥、生产部署和回滚进入确认状态，不直接执行。

实时天气、新闻、价格、比赛、联网搜索、网页读取等问题会以 `read` 模式转给
Hermes 工具层。普通模型如果生成“无法获取实时数据、不能联网或不能访问网页”
一类能力失败回复，也会在发送到群前被拦截，自动改由 Hermes 查询并直接返回
自然语言答案。代码修改、部署等操作仍按 `write/high` 权限处理。

“1分钟后提醒我”“每天早上9点执行”“暂停/恢复/删除定时任务”等表达会直接
进入 Hermes Cron。任务触发时先调用 AI 服务的本机 `/automation/deliver` 入口，
统一归档后再通过现有 OneBot 回到来源群，不会启用 Hermes 自带微信通道。
项目所有者由 `HERMES_OWNER_USER_IDS` 显式配置，
可以在所有已接入群创建定时任务；其他成员仍按群管理员权限判断。

每个自动化任务在 SQLite 中保存发起群、成员、原消息、Hermes run ID、工具事件
和最终结果，并每 2 秒镜像到 PostgreSQL。创建任务后立即回执，最终结果异步回群。

## Hermes 高并发与缓存

Hermes 任务按风险和用途拆为四个互不阻塞的工作池：

| 工作池 | 默认线程 | 用途 |
| --- | ---: | --- |
| `read` | 16 | 天气、行情、新闻、搜索、网页读取 |
| `cron` | 6 | 提醒、定时任务的创建和管理 |
| `ops` | 8 | GitHub、测试、构建、部署和监控 |
| `high` | 2 | 删除、强推、密钥、生产部署和回滚 |

Hermes API Server 同时允许 32 个并发 Run，项目侧队列默认容量 1000。普通只读
结果写入 SQLite 并跨重启复用：默认 24 小时、天气 1 小时、行情 15 分钟、新闻
6 小时、状态 2 分钟。用户明确说“强制刷新”“重新查询”或“不要缓存”时跳过缓存。

群内消息体验固定为：

| 耗时 | 群内表现 |
| --- | --- |
| 0～10 秒 | 静默等待，只发送最终答案 |
| 10～30 秒 | 仍优先只发送最终答案 |
| 超过 30 秒 | 发送一次“正在查询”，完成后发送最终答案 |
| 代码、部署、长任务 | 立即发送任务已接收 |
| 高风险操作 | 立即发送等待审批 |

所有参数都可以在 `http://127.0.0.1:8765/#memory-infra` 调整。只读查询、Cron、
开发运维和高风险任务使用不同队列，任一类任务积压不会占满其他类别的线程。

## 后台控制台

打开 `http://127.0.0.1:8765/#memory-infra` 可以直接：

- 调整 250ms 检索截止、上下文预算、快速路由模型和超时。
- 启停可靠同步、Graphiti 与 Hermes，并设置批量、轮询和任务超时。
- 重试同步/关系图任务，执行中央记忆备份。
- 打开 MinIO 与 FalkorDB 管理界面。
- 手动创建 Hermes 任务，审批高风险任务，停止排队或运行中的任务。

Hermes 有两套独立入口：

- API Gateway：`http://127.0.0.1:8642`，只提供程序接口，根路径返回 404。
- 官方 Web Dashboard：`http://127.0.0.1:9119`，提供 Chat、Sessions、Models、
  Cron、Skills、Plugins、MCP、Config、Keys 和 System 等完整管理页面。

项目后台的“打开真正的 Hermes WebUI”会直接打开官方 Dashboard；页面下方的
“Hermes 任务控制台”只负责微信机器人侧的任务创建、审批、停止和结果查看。

## 当前完成度

已完成：可靠 SQLite outbox、PostgreSQL/PGroonga/pgvector 结构、MinIO 归档、
250ms 降级链路、Graphiti/FalkorDB 后台桥接、Hermes API/权限/审批/停止、
备份脚本和可操作后台。

仍需继续验收：

- 当前微信账号的完整历史范围尚未证明全部导入，必须按群核对最早/最晚时间和媒体完整度。
- PostgreSQL `memory_embeddings`、`memory_facts`、`memory_summaries` 已接通异步回填，
  仍需等待历史队列清零并记录最终数量。
- Graphiti 历史任务需要清完积压并解决上游模型偶发 502。
- `grok-chat-fast` 上游仍不可用；快速路由已切换 `deepseek-v4-flash` 并通过真实测试。
- 2026-07-24 的 37 条真实回复样本为 P50 20.893 秒、P95 28.145 秒，尚未达到
  P50 2.5 秒/P95 5 秒目标，需继续定位模型首包和最终回复耗时。
- 备份已执行，仍需做一次隔离恢复演练并记录恢复结果。
- Hermes 只读任务、高风险审批/停止，以及 Cron 创建/暂停/恢复/删除已实测；
  真实代码写入、GitHub 推送和生产部署仍需分别验收。
