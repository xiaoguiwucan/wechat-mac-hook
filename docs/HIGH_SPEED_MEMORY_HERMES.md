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

当前机器未启动 8B 本地 Embedding/Reranker，`embedding.enabled=false`。全文、
缓存和 Graphiti 继续工作，PostgreSQL 的 pgvector 结构保留给后续云端 Embedding
回填，不消耗大内存本地模型。

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

# 备份 SQLite、PostgreSQL 和 MinIO 对象清单
./scripts/backup_durable_memory.sh
```

导入器只读打开快照，不接触运行中微信数据库；重复执行依靠稳定 `event_id`
去重。PostgreSQL 还使用账号、群、方向、消息 ID、时间的唯一约束消除实时记录
与历史快照的重复。

## Hermes 权限

- `read`：群成员可以查询状态。
- `write`：必须是后台配置的群管理员。
- `high`：删除、强推、密钥、生产部署和回滚进入确认状态，不直接执行。

每个自动化任务在 SQLite 中保存发起群、成员、原消息、Hermes run ID、工具事件
和最终结果。创建任务后立即回执，最终结果异步回群。

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
- PostgreSQL `memory_embeddings`、`memory_facts`、`memory_summaries` 仍需异步回填。
- Graphiti 历史任务需要清完积压并解决上游模型偶发 502。
- `grok-chat-fast` 需恢复真实可用后再作为主路由；不可用期间使用本地规则降级。
- 2026-07-24 的 37 条真实回复样本为 P50 20.893 秒、P95 28.145 秒，尚未达到
  P50 2.5 秒/P95 5 秒目标，需继续定位模型首包和最终回复耗时。
- 备份已执行，仍需做一次隔离恢复演练并记录恢复结果。
- Hermes 只读任务和高风险审批/停止已实测；定时任务创建/暂停/删除、真实代码
  写入、GitHub 推送和生产部署仍需分别验收。
