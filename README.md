# 企业微信本地消息采集与只读 MCP

当前实现已在企业微信 Windows 5.0.12.6018、32 位 WXWork 进程上验证。它读取**当前登录账号已经同步到本机的消息**，不需要企业管理员或会话存档权限，不新增登录、不使用小号、不注入进程，也不修改企业微信原始数据库。部署包面向无外网环境，内含离线 wheel，不要求访问 PyPI 或 GitHub。

## 本次验收结果

- 16 个加密数据库通过密钥校验；快照通过 SQLite `integrity_check`。
- WAL 校验 header/frame checksum，只应用到最近 commit，并按 commit `dbsize` 截断。
- 本次源消息表共 12,724 行；规范化后 **12,702 条消息**，移除 22 条重复记录。
- 共 191 个有消息的会话；`msgid` 无重复，`sender_id` 全部写入，`msgtype` 物理类型为 INTEGER。
- MCP 的 initialize、tools/list、tools/call、JSON-RPC batch、中文搜索、完整读取、分页和强制 GBK 环境下的 UTF-8 stdio 均已验证。
- 离线 DB/WAL 复制、checkpoint、完整性及 schema 校验均有合成回归测试。

这些数字是本次验证时的快照，不代表服务器上的全部历史或其他账号的数据；后续新消息会改变总数。

## 一键部署

当前 Windows 企微端只需双击 `01_一键部署.cmd`。目标机需已有 64 位 Windows CPython 3.9+；入口自动创建项目内 `.venv`，使用 `--no-index` 从随包 `wheels/` 安装依赖，再执行企微目录配置、密钥校验和首次同步，全程不访问外网。也可在命令行执行：

```powershell
& ".\01_一键部署.cmd"
```

缺少密钥或出现明确的密钥错误时，入口会在本地重新提取并验证，随后生成：

1. `mcp_config.generated.json`：手工导入 Agent 的 MCP 配置。
2. `02_启动消息采集.cmd`：启动持续采集。
3. `03_启动只读MCP.cmd`：启动本地只读 MCP。

部署后立即启动持续采集：

```powershell
& ".\01_一键部署.cmd" -StartWatcher
```

采集异常写入 `logs/collector.log` 并指数退避重试；隐藏启动的 stdout/stderr 分别写入 `data/collector.stdout.log` 和 `data/collector.stderr.log`。脚本不会修改 Agent 配置、开放网络端口或让 MCP 启动时自动提取密钥。

## 手工运行

```powershell
# 刷新数据库/WAL 快照
python wecom_snapshot.py

# 全量重扫并 UPSERT；新增数不等于扫描数
python wecom_collector.py --once

# 持续采集，默认每 15 秒一轮
python wecom_collector.py --watch --poll 15

# 启动 stdio MCP
python wecom_mcp.py
```

重新导出本地快照：

```powershell
cd third_party\wecom-decrypt
python export_wxwork_messages.py --formats json
cd ..\..
```

密钥失效时，保持企微登录，在 `third_party\wecom-decrypt` 执行 `python find_wxwork_keys.py`。密钥文件 `wxwork_keys.json` 属于敏感文件，不要上传或放入部署 ZIP。

## 消息身份与更新语义

- 有非零 `server_id` 时，使用 `conversation_id + server_id` 形成 v2 canonical ID；跨 `message_table`、`message_small_table` 的同一消息只保留一条，主表优先。
- `server_id=0` 时使用来源表和本地 `message_id` 兜底。
- 旧 SQLite 会自动迁移并合并重复记录；`sender_id` 独立保存。
- 每轮全量重扫并 UPSERT，可覆盖低位点晚到、正文变化和撤回标志；物理删除不会自动删除下游行。
- 全量重扫复杂度为 O(N)，适合当前本地规模；大型部署应另外评估分区/增量策略。
- 发送人名称能从联系人/群成员映射取得时使用名称；无法取得时保留数字 ID，不虚构名称。MCP 可按名称或 ID 搜索。

## 本地 MCP

`wecom_mcp.py` 使用 MCP **2024-11-05** 和换行分隔 JSON-RPC 2.0，支持 batch。stdin/stdout/stderr 始终使用显式 UTF-8 字节流，不依赖 Windows ANSI/GBK 区域设置。只使用 stdio，不开放 TCP 端口。

默认只读工具：

- `list_conversations`：会话、类型、去重后的消息数和最近时间。
- `search_messages`：正文关键词，支持会话、发送人名称/ID、时间范围和分页。
- `recent_messages`：最近消息，支持会话和分页。
- `get_message`：按 `msgid` 读取完整正文及解析字段。

列表和查询均返回：`count`（本页条数）、`total`（总命中数）、`offset`、`has_more`、`next_offset`。消息的 `msgtype` 为整数，另提供 `type_name`。

时间采用**半开区间**：`start_time` 包含，`end_time` 不包含。date-only 的结束日期包含该本地日期整天，内部转换为次日午夜；带偏移 ISO 按偏移转换，无偏移 ISO/date-only 按服务器本地时区。

默认 `WECOM_MCP_READ_ONLY=1`，不展示 `sync_messages`。只有明确设置 `WECOM_MCP_READ_ONLY=0` 后才可显式调用同步工具。MCP 启动、搜索和读取不会扫描内存或提取密钥。

## 离线迁移与生产环境

**不要只复制一个仍处于 WAL 模式的主库文件。**使用检查工具复制 DB+WAL、在副本上 checkpoint，并验证完整性和 schema：

```powershell
python prepare_offline_db.py "D:\source\wecom_messages.sqlite" "D:\target\wecom_messages.sqlite"
```

或直接离线部署：

```powershell
& ".\01_一键部署.cmd" -OfflineDb "D:\source\wecom_messages.sqlite"
```

离线 MCP 只需要准备后的 SQLite，不需要企微、密钥或 `decrypted/`。设置 `WECOM_MCP_DB` 指向该文件即可。

换机继续采集时，新机器必须运行已登录并同步消息的企微；复制 `wecom_config.example.json` 为实际配置、通过 `WECOM_CONFIG` 指定，并重新验证当前版本的密钥提取。旧机器的密钥和内存地址不能当作通用配置。当前解密和 WAL 实现要求 4096 字节页，其他页大小会快速失败，不会静默继续。

其他 Agent 的权限审批、DLP、脱敏或容器隔离可能影响工具调用和正文展示；这与 MCP 协议无关。默认只读、无外发、同步工具隔离可减少权限面。

## Agent Skill、Qoder 与 WorkBuddy

随包提供 `skills/wecom-local-mcp/SKILL.md`，Agent 可按该技能自助完成同一个 BAT 的部署、配置导入、协议验证和消息查询，不需要执行联网安装或逐条运行内部脚本。

- Qoder：`skills/wecom-local-mcp/references/qoder.md` 与 `qoder.mcp.json`，支持当前版本的 JSON 导入或 STDIO 表单。
- WorkBuddy：`skills/wecom-local-mcp/references/workbuddy.md` 与 `workbuddy.mcp.json`，支持用户级或项目级 MCP 配置。
- 两者都以部署后生成的 `mcp_config.generated.json` 为权威来源，不分别手工猜测路径。
- 可用 `skills/wecom-local-mcp/scripts/verify_agent_deploy.py --require-data` 验证 initialize、四个只读工具和完整消息读取。

## 测试与打包

```powershell
python -m unittest -v test_pipeline.py test_collector_regressions.py test_snapshot_regressions.py test_mcp_regressions.py
python -m unittest -v test_deploy_regressions.py
python test_mcp.py
python package_zip.py
```

覆盖跨表重复、晚到/撤回更新、WAL checksum/commit/dbsize、未提交尾部、离线 checkpoint、分页、时间边界、batch、中文 stdio、部署工作目录和中文启动器。`test_deploy_regressions.py` 含打包自检，单独运行以避免递归。

`package_zip.py` 生成的 ZIP 不含本机 `.venv`、密钥、聊天数据库、解密快照、导出、日志或截图。它包含离线 wheel 和 Agent Skill，并会在临时目录解压后执行禁网 venv 安装、非递归回归、合成 MCP 内容校验和 Skill/宿主模板检查。

## 范围限制

1. 只能覆盖当前账号已同步到本机的消息；服务器未同步、已清除或客户端不可见的历史无法由本地库恢复。
2. 新消息采集需要电脑开机、企微运行并同步；持续采集不等于离线服务器拉取。
3. 文件/图片消息记录不等于附件正文；附件需从本地缓存或已下载文件另行处理。
4. 本地快照、SQLite 和密钥都应按敏感数据保管。早期 `notify.py`、会话存档 API 等原型不在本验证链路中。
