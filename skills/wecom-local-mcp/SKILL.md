---
name: wecom-local-mcp
description: 在 Windows 离线或受限网络环境中部署、注册、验证并使用企业微信本地只读消息 MCP。适用于 01_一键部署.cmd、自动 venv、本地 wheel、OfflineDb、Qoder/WorkBuddy 接入、MCP 协议验证、消息搜索与排错。
---

# 企业微信本地消息 MCP

通过随包唯一部署入口完成离线安装，再把生成的 stdio MCP 配置交给当前 Agent 宿主。默认只读，不访问外网，不开放端口。

## 不可绕过的边界

- 部署只调用包根目录的 `01_一键部署.cmd`。不要直接调用 `deploy_wecom.ps1`、`setup_venv.py`、密钥提取器或采集器，也不要执行联网 `pip install`。
- 入口自动创建 `.venv`，并用 `--no-index` 从随包 `wheels/` 安装依赖。缺少 wheel 时补齐原始离线包，不改用公网源。
- 不擅自新建、覆盖或修补 Agent 宿主配置。只使用生成的 `mcp_config.generated.json` 进行导入；需要手工合并时，先取得用户同意，且只合并 `mcpServers["wecom-local-messages"]`。
- 不上传或要求用户粘贴密钥、消息正文、SQLite/DB/WAL、解密快照、导出或日志。不要把 `wxwork_keys.json`、数据库或消息内容打入 ZIP。
- 保持 `WECOM_MCP_READ_ONLY=1`，不要启用 `sync_messages`。MCP 仅走 stdio；不要增加 HTTP/SSE 服务、监听器、端口转发或外发代理。
- 默认不启动持续采集。只有用户明确要求持续采集时，才使用部署后生成的采集启动器。

## 1. 定位并检查离线包

将 `PACKAGE_ROOT` 设为解压后包含 `01_一键部署.cmd` 的目录，而不是本 Skill 所在目录。后续命令均在该目录执行。

1. 确认 `01_一键部署.cmd`、`wheels/`、`wecom_mcp.py` 和 `mcp_config.example.json` 存在。
2. 确认目标机已有可用的 64 位 Windows Python，且 `python` 或 `py -3` 可启动它。不要在线下载 Python 或依赖；wheel 必须与解释器版本和架构匹配。
3. 只检查本任务明确涉及的路径，不枚举无关用户目录、浏览器数据或凭据目录。

## 2. 通过唯一入口部署

Agent 或非交互终端先设置 `$env:WECOM_DEPLOY_NO_PAUSE = "1"`，让 CMD 结束后立即返回退出码；人工双击部署时可保留其暂停提示。

### 模式 A：读取本机企微已同步消息

前置条件：当前 Windows 用户已登录企业微信，客户端正在运行，所需消息已经同步到本机。部署只覆盖本机已有数据，不等于拉取服务器全部历史。

1. 优先让入口自动检测企微配置。若输出明确要求配置，按输出指定的包内配置填写 `wxwork_db_dir`，或通过同一入口传入 `-ConfigPath`；不要手工运行密钥工具。
2. `wxwork_keys_file` 指向本地密钥文件。密钥缺失或明确失效时，由入口在本地提取、验证并重试；不要复制其他机器或旧版本的密钥。
3. 在 PowerShell 中执行并检查退出码：

```powershell
Set-Location -LiteralPath "C:\path\to\wecom-local-mcp"
$env:WECOM_DEPLOY_NO_PAUSE = "1"
& ".\01_一键部署.cmd"
if ($LASTEXITCODE -ne 0) { throw "WeCom deployment failed: $LASTEXITCODE" }
```

需要显式配置时仍只使用同一入口：

```powershell
& ".\01_一键部署.cmd" -ConfigPath "C:\path\to\wecom-local-mcp\third_party\wecom-decrypt\config.json"
```

### 模式 B：使用 OfflineDb

适用于只查询一份已规范化的本地消息 SQLite，不读取企微、不提取密钥。输入应具有 `messages` 表；若源库仍有 WAL，让同目录的 `-wal`/`-shm` 保持可读，由入口复制、checkpoint 并校验，不要只徒手复制 WAL 模式的主库。

```powershell
Set-Location -LiteralPath "C:\path\to\wecom-local-mcp"
$env:WECOM_DEPLOY_NO_PAUSE = "1"
& ".\01_一键部署.cmd" -OfflineDb "D:\offline\wecom_messages.sqlite"
if ($LASTEXITCODE -ne 0) { throw "OfflineDb deployment failed: $LASTEXITCODE" }
```

OfflineDb 应是本工具链准备或验证过的消息库，不是企微原始加密数据库。入口生成只读副本及指向它的 MCP 配置；源库路径变化不会自动刷新该副本。

### 部署完成判据

同时满足以下条件再进入注册：

- 入口退出码为 0，并显示完成信息。
- `.venv/Scripts/python.exe` 存在，依赖已从本地 wheel 安装。
- 包根目录生成 `mcp_config.generated.json`。
- 生成配置的 `command`/`args` 指向本包运行环境，`WECOM_MCP_DB` 指向本次数据库，`WECOM_MCP_READ_ONLY` 为 `"1"`。

若入口失败，保留退出码和错误类别，按“排错”处理；不要绕过入口逐条手工执行内部步骤。

## 3. 导入 MCP 配置

1. 打开包根目录的 `mcp_config.generated.json`，优先使用宿主提供的“导入 MCP JSON”功能。
2. 若宿主要求手工合并，经用户同意后仅加入以下节点，保留宿主已有服务器和设置：

```text
mcpServers["wecom-local-messages"]
```

3. 保留生成配置中的绝对路径及 `WECOM_MCP_DB`、`WECOM_MCP_READ_ONLY`、`PYTHONUTF8`、`PYTHONIOENCODING` 环境变量。
4. 按宿主要求重连或重启 MCP 客户端。移动包目录后，重新通过唯一入口部署并重新导入，不手工猜测新路径。
5. 宿主配置不可写时，不修改 ACL、不提升权限、不改其他配置；把导入这一步交给用户完成。

宿主专用步骤和可复制模板：

- **Qoder**：[references/qoder.md](references/qoder.md)。使用当前版本的 Connectors/MCP JSON 导入或 STDIO 表单；未确认的磁盘配置路径以 UI 为准。
- **WorkBuddy**：[references/workbuddy.md](references/workbuddy.md)。使用“插件 → MCP 服务器 → 配置 MCP”，并按用户级或项目级导入。

两个宿主都直接复用同一个 `mcp_config.generated.json`，不要分别维护内容不一致的配置。

## 4. 验证协议与只读工具

部署脚本完成后，运行随 Skill 的本地验证器。它直接读取生成配置并启动 stdio 子进程，不开放网络，也不打印消息正文：

```powershell
& ".\.venv\Scripts\python.exe" ".\skills\wecom-local-mcp\scripts\verify_agent_deploy.py"
```

需要非空数据库并完成一次 `get_message` 成功读取时：

```powershell
& ".\.venv\Scripts\python.exe" ".\skills\wecom-local-mcp\scripts\verify_agent_deploy.py" --require-data
```

验证器检查：

- `initialize` 返回 MCP `2024-11-05`，服务名为 `wecom-local-messages`。
- `tools/list` 包含 `list_conversations`、`search_messages`、`recent_messages`、`get_message`，且不包含 `sync_messages`。
- `list_conversations`、`recent_messages` 和 `search_messages` 可调用；有消息时验证 `get_message`。

随后在 Agent 宿主中重连，并确认能看到同名四个只读工具。宿主的权限审批或 DLP 拦截属于宿主行为，不要通过修改协议或关闭只读来绕过。

## 5. 使用消息工具

按“缩小范围 → 分页查询 → 按 ID 读取”的顺序，避免无关批量导出：

1. `list_conversations`：按名称或 ID 找会话，取得 `conversation_id`。
2. `search_messages`：用正文关键词查询，可叠加 `conversation_id`、`sender`、`start_time`、`end_time`、`limit`、`offset`。
3. `recent_messages`：查看全部或单个会话的最近消息。
4. `get_message`：使用查询结果中的 `msgid` 读取完整正文和解析字段。

列表与查询结果包含 `count`、`total`、`offset`、`has_more`、`next_offset`。当 `has_more=true` 时，以 `next_offset` 请求下一页。时间采用 `start_time` 包含、`end_time` 不包含的半开区间；date-only 的结束日期表示包含该本地日期整天。

详细参数和示例见 [references/usage.md](references/usage.md)。只查询回答当前问题所需的最小范围；本地没有、未同步或已清除的消息不能由 MCP 恢复，文件/图片记录也不等于附件正文。

## 6. 权限不足与敏感信息处理

遇到读取企微 Data、读取 OfflineDb、写入 `.venv`/`data`、生成配置或导入宿主配置被拒绝时：

1. 记录具体动作、目标路径、原始错误类别和退出码，不记录密钥或消息正文。
2. 不提升权限、不接管文件、不改 ACL、不关闭宿主审批，也不改写到其他宿主配置。
3. 请用户在其当前会话运行同一 `01_一键部署.cmd`，或提供可写的包目录和可读的明确数据路径；随后从唯一入口重试。
4. 密钥或消息需要展示给 Agent 之外的系统时，先由用户明确选择范围并进行脱敏；默认不传输。

## 7. 排错

| 现象 | 处理 |
|---|---|
| 找不到 Python / `python` 不在 PATH | 使用目标机已有 Python 并让用户修正 PATH，或确认 `py -3` 可用；不要联网下载。 |
| 非交互运行停在按键提示 | 设置 `WECOM_DEPLOY_NO_PAUSE=1` 后重新调用同一 CMD，并检查退出码。 |
| 缺少 `.venv`、wheel 或出现 `--no-index` 依赖错误 | 重新取得完整离线 ZIP，确认 `wheels/*.whl` 与目标 Python/Windows 架构匹配，再运行唯一入口。 |
| PowerShell 执行策略报错 | 从 `01_一键部署.cmd` 进入；不要直接运行内部 PS1，也不要修改机器级执行策略。 |
| 写入 `.venv`、`data` 或包根目录被拒绝 | 按“权限不足”处理，由用户提供可写目录或本地运行入口。 |
| 未找到企微 Data / 自动检测失败 | 确认当前账号已登录并同步；仅修正包内 `wxwork_db_dir` 或通过 `-ConfigPath` 指定，再由入口重试。 |
| 缺少密钥、密钥校验失败或解密失败 | 保持企微运行、登录和同步，重新运行唯一入口一次，让其在本地刷新并验证密钥；不要上传密钥或手工扫描进程。持续失败时记录企微版本和错误类别。 |
| OfflineDb 报 schema、integrity、page size 或 WAL 错误 | 使用本工具链验证过的规范化消息库及配套 WAL；不要传入原始加密库。当前解密/WAL 链路要求 4096 字节页。 |
| MCP 不出现或宿主连接失败 | 检查生成配置的绝对路径，重新导入并按宿主要求重连；不要覆盖宿主其他节点。 |
| initialize/tools 无响应、中文乱码或协议错误 | 运行验证器，保留生成配置中的 UTF-8 环境变量；stdout 只用于 JSON-RPC，不向其中写入诊断文本。 |
| 查询为空或数据未更新 | 先确认本机是否已同步。在线模式可重新运行唯一入口做一次同步；OfflineDb 是静态副本，需要重新部署新副本。 |
| `search_messages` 提示缺少 query | 提供非空关键词；只看最近消息时改用 `recent_messages`。 |
| 移动目录后路径失效 | 在新目录重新运行唯一入口并重新导入生成配置。 |
