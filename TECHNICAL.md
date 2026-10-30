# 企业微信本地消息采集与只读 MCP

当前实现已在企业微信 Windows 5.0.12.6018、32 位 WXWork 进程上验证。它读取**当前登录账号已经同步到本机的消息**，不需要企业管理员或会话存档权限，不新增登录、不使用小号、不注入进程，也不修改企业微信原始数据库。部署包面向无外网环境，内含离线 wheel，不要求访问 PyPI 或 GitHub。

## 本次验收结果

- 16 个加密数据库通过密钥校验；快照通过 SQLite `integrity_check`。
- WAL 校验 header/frame checksum，只应用到最近 commit，并按 commit `dbsize` 截断。
- 本次源消息表共 12,980 行；规范化后 **12,958 条消息**，移除 22 条重复记录。
- 共 192 个有消息的会话；`msgid` 无重复，`sender_id` 全部写入，`msgtype` 物理类型为 INTEGER。
- MCP 的 initialize、tools/list、tools/call、JSON-RPC batch、中文搜索、完整读取、分页和强制 GBK 环境下的 UTF-8 stdio 均已验证。
- 离线 DB/WAL 复制、checkpoint、完整性及 schema 校验均有合成回归测试。
- v1.2.1 修正 protobuf 嵌套字段解析：不再把字段标签当成正文首字母，同时避免把正常文件名误判为 protobuf。
- v1.2.2 收紧纯文本快速通道，修复 `eh08` 等紧凑 protobuf 残留前缀；图片等二进制消息不再猜测 GBK/UTF-16，优先显示清理后的本地附件路径。
- v1.2.3 恢复单字消息解析，ASCII、中文和 emoji 单字不再退化为二进制占位。
- v1.3.0 增加 flag 迁移与按位过滤、结构化和文本 @提及、时间窗空查询、内容重复键/计数、count_messages 多维聚合及五工具自动验证。
- 大库优化：快照指纹与基线复用、高位点+512 回看、默认 3600 秒周期全量、rooms 预聚合、external-content trigram FTS（rowid 映射），短查询/旧库回退 instr；dup_count 查询使用 roomid+msgtime 复合索引。
- v1.4.0 增加标准库实现的本地只读 WebUI（127.0.0.1:8765）、统计/搜索/详情/聚合 API、离线页面和中文启动器，无外部 CDN 或 Web 框架依赖。

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

重新导出本地快照（一次生成三种格式）：

```powershell
cd third_party\wecom-decrypt
python export_wxwork_messages.py --formats csv,html,json
cd ..\..
```

`--formats` 接受逗号分隔的 `csv`、`html`、`json`，未提供时默认只导出 CSV。先运行 `python export_wxwork_messages.py --list` 可查看会话 ID；再用一个或多个 `--conversation "R:example"` 限定导出范围。默认读取 `wxwork_decrypted_dir`，写入 `wxwork_export_dir`（默认为 `third_party/wecom-decrypt/wxwork_export`），每个会话一个目录：

- `messages.csv`：UTF-8 with BOM，便于 Excel 打开，包含展示正文、身份字段和 `flag`。
- `messages.html`：静态可读聊天记录，按日期分组并区分发送/接收。
- `messages.json`：UTF-8 结构化数据，包含会话、消息数和完整解析字段。

导出会按 canonical 消息身份消除跨表重复，但不会把附件转换成正文。导出目录包含敏感消息，和密钥、数据库一样不应上传或打入发布 ZIP。

密钥失效时，保持企微登录，在 `third_party\wecom-decrypt` 执行 `python find_wxwork_keys.py`。密钥文件 `wxwork_keys.json` 属于敏感文件，不要上传或放入部署 ZIP。

## 消息身份与更新语义

- 有非零 `server_id` 时，使用 `conversation_id + server_id` 形成 v2 canonical ID；跨 `message_table`、`message_small_table` 的同一消息只保留一条，主表优先。
- `server_id=0` 时使用来源表和本地 `message_id` 兜底。
- 旧 SQLite 会自动迁移并合并重复记录；`sender_id` 独立保存。v1.3 增加 `messages.flag INTEGER NOT NULL DEFAULT 0`，历史行从 `raw.flag` 回填，MCP 对旧 schema/raw 仍有兜底。
- 类型 1002 的 @提及会把正文中的用户 ID 解析为 `raw.at_user_id` 和 `raw.at_user` 数组；联系人名称不可用时保留数字 ID。
- 每轮全量重扫并 UPSERT，可覆盖低位点晚到、正文变化和撤回标志；物理删除不会自动删除下游行。
- 全量重扫复杂度为 O(N)，适合当前本地规模；大型部署应另外评估分区/增量策略。
- 发送人名称能从联系人/群成员映射取得时使用名称；无法取得时保留数字 ID，不虚构名称。MCP 可按名称或 ID 搜索。

## 本地 MCP

`wecom_mcp.py` 使用 MCP **2024-11-05** 和换行分隔 JSON-RPC 2.0，支持 batch。stdin/stdout/stderr 始终使用显式 UTF-8 字节流，不依赖 Windows ANSI/GBK 区域设置。只使用 stdio，不开放 TCP 端口。

默认只读工具：

- `list_conversations`：会话、类型、去重后的消息数和最近时间。
- `search_messages`：正文关键词可选；省略或留空时保留会话、发送人、消息类型、时间和 `flag` 过滤，不再要求必须有正文条件。
- `recent_messages`：最近消息，支持会话、发送人、消息类型、时间范围、`flag` 和分页。
- `count_messages`：按 `conversation`、`sender`、`msgtype` 或 `day` 聚合消息数量，支持时间窗。`group_by` 接受字符串、逗号分隔字符串或字符串数组，默认 `conversation`；还可使用 `query`、`conversation_id`、`sender`、`msgtype`、`start_time`、`end_time`、`flag_mask`、`flag_value` 过滤。返回 `{total,count,group_by,groups}`，其中 `total=count=过滤后的消息记录数`，不是分组数；`groups` 每行含 `count`，按维度另含 `conversation_id`、`sender+sender_id`、`msgtype+type_name` 或本地日期 `day(YYYY-MM-DD)`。
- `get_message`：按 `msgid` 读取完整正文及解析字段。

列表和分页查询均返回：`count`（本页条数）、`total`（总命中数）、`offset`、`has_more`、`next_offset`。消息的 `msgtype` 为整数，`type_name` 优先采用快照中的 `raw.type_name`，否则使用本地类型表；v1.3 补充 13（在线文档）、20（媒体消息）、29（表情）、35（图文链接）、36（微信/外部消息）、123（混合消息）和 1002（@提及）。

消息摘要还包含：

- `flag` / `flag_bits`：原始整数位标记及从 0 开始的置位位置列表（`5 → [0,2]`）；`search_messages`、`recent_messages`、`count_messages` 使用 `(flag & flag_mask) == flag_value` 过滤。提供掩码时 `flag_value` 默认 0，单独提供 `flag_value` 报错。位含义取决于消息类型和客户端版本，不统一解释为已读或撤回。
- `dedupe_key` / `dup_count`：对 JSON `[conversation_id,msgtime,normalized_content]` 做 SHA-256，正文 trim 并折叠连续空白；`dup_count` 是全库同键的不同 `msgid` 数，不受当前过滤/分页影响。只标注，不删除记录，也不改变分页或聚合计数。
- `at_user_id` / `at_user`：@联系人 ID 和带 @ 显示文本数组；`mentions` 为 `{user_id,name}` 数组，`mention_all` 由 ID、名称或正文线索推导。当前真实 1002 样本未观察到 @all 哨兵，不能据此假定所有企微版本使用相同编码。

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
- 可用 `skills/wecom-local-mcp/scripts/verify_agent_deploy.py --require-data` 验证 initialize、核心只读工具和完整消息读取；`count_messages` 另通过 `tools/list` 和一次聚合调用核对。

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
