# 企业微信消息助手（本地 MCP）

让 Qoder、WorkBuddy 或其他支持 MCP 的 Agent，直接搜索和读取你电脑里已经同步的企业微信消息。

**不用企业管理员权限，不用小号，不上传聊天记录。**第一次部署也不需要访问外网。

## 它能做什么

- 按关键词搜索消息，例如 `eh08`、`JPH`、文件名或某人说过的话。
- 不提供关键词时，也能按会话、发送人、时间和消息标记筛选。
- 查看某个群的最近消息，再按消息 ID 读取完整内容。
- 按会话、发送人、消息类型或日期统计消息数量。
- 查看消息的 `flag` 标记、@提及信息和重复组线索。
- 显示文本、应用消息和本地附件路径。
- 默认只读，不会以你的身份发送消息。

## 安装：只点一个文件

### 准备

- 64 位 Windows，已安装 64 位 Python 3.9 或更高版本。
- 企业微信已登录，并且需要的消息已经同步到这台电脑。
- 一个可写的解压目录。

### 开始

1. 下载并解压 [最新离线包](https://github.com/sutanna233/wecom-local-mcp/releases/latest)。
2. 双击 **`01_一键部署.cmd`**。
3. 等窗口显示完成。程序会自动创建本地环境、检测企微数据目录并生成 `mcp_config.generated.json`。
4. 把这个 JSON 导入你的 Agent 宿主：
   - [Qoder 接入说明](skills/wecom-local-mcp/references/qoder.md)
   - [WorkBuddy 接入说明](skills/wecom-local-mcp/references/workbuddy.md)

不需要自己创建虚拟环境，也不需要执行 `pip install`。依赖已经放在包里。

## 让 Agent 这样用

接入后，可以直接说：

> 查找“安全现场管理群”里包含 eh08 的消息。

> 列出最近有消息的 10 个会话。

> 找出孙乐乐最近发来的 5 条消息。

> 搜索所有包含 B216每日JPH区域.xlsx 的记录，并读取完整消息。

> 不用关键词，列出“项目群”在 2026-10-01 当天的消息。

> 统计本月各会话的消息数，再按发送人查看“项目群”的分布。

Agent 会使用这些只读工具：

| 工具 | 用途 |
|---|---|
| `list_conversations` | 查找会话、群聊和联系人 |
| `search_messages` | 按正文（可留空）、会话、发送人、类型、时间和 `flag` 筛选 |
| `recent_messages` | 查看最近消息，支持会话、发送人、类型、时间和 `flag` 筛选 |
| `count_messages` | 按会话、发送人、消息类型或日期聚合数量 |
| `get_message` | 按 `msgid` 读取完整消息、@提及和重复组信息 |

### 看懂标记、重复组和 @提及

- `flag` 是企微消息自带的整数位标记，`flag_bits` 展示置位信息。高级查询可用掩码和值组合筛选；不同消息类型或客户端版本的位含义可能不同，不要自行把某一位直接等同于“已读”或“撤回”。
- `dedupe_key` 和 `dup_count` 用“同会话、同秒、规范化正文相同”发现重复组，只提供线索，不会删除记录；`dup_count` 是全库同键的不同 `msgid` 数量。它与采集阶段依据稳定 `msgid` 消除跨表重复是两层机制。
- `at_user_id`、`at_user` 和 `mentions` 保留被 @ 的联系人，`mention_all` 表示 @所有人。能解析到联系人时显示名称，否则保留 ID。

## 打开本地 WebUI

部署后双击 `04_启动消息WebUI.cmd`，或运行 `python wecom_webui.py`，然后访问 **http://127.0.0.1:8765**。页面提供消息/会话统计、采集状态、组合查询、分页、完整消息详情和聚合图表。WebUI 与 MCP 共用本地数据库和索引，默认只读且只监听本机回环地址，不需要额外安装 Web 框架。

## 如果你只有一份消息数据库

可以用同一个 BAT 部署离线查询，不需要启动企微：

```powershell
& ".\01_一键部署.cmd" -OfflineDb "D:\offline\wecom_messages.sqlite"
```

如果数据库旁边还有 `-wal` 文件，请一起保留。程序会在副本上完成 checkpoint 和完整性检查，不修改源文件。

## 导出 CSV、HTML 或 JSON

需要人工阅读或交给其他本地工具处理时，可以从已解密的本地快照导出。进入目录后执行：

```powershell
cd third_party\wecom-decrypt
python export_wxwork_messages.py --formats csv,html,json
```

- `--formats` 可填 `csv`、`html`、`json` 的任意逗号分隔组合；省略时只生成 CSV。
- 默认写入 `wxwork_export`；每个会话目录中分别生成 `messages.csv`、`messages.html` 或 `messages.json`。
- CSV 适合表格处理，HTML 适合直接阅读，JSON 适合保留结构化字段。
- 先用 `python export_wxwork_messages.py --list` 查看会话，再用重复的 `--conversation "R:example"` 参数限定范围。

导出文件同样包含聊天记录，不要把它们上传到网盘、Issue 或公开仓库。

## 数据与隐私

- 消息只在本机读取和保存，MCP 只走本地 stdio，不开放网络端口。
- 不要把 `wxwork_keys.json`、消息数据库、`decrypted/` 或导出目录上传给别人。
- 本工具只能读取**已经同步到本机**的消息，不能恢复服务器上从未同步、已删除或不可见的历史。
- 新消息需要企微运行并同步；持续采集不等于从服务器补拉全部历史。在线部署默认启动或复用后台采集器，静态 OfflineDb 不启动；只部署一次时可传 `-NoWatcher`。
- 找不到联系人或群成员名称时，`sender` 会保留数字 ID，不代表查询失败；可按显示名或发送人 ID 筛选。
- 后台采集采用高位点增量和回看窗口，并定期全量校验；源文件不变时跳过。全量校验默认每小时一次，可用 `--full-interval` 调整。源端物理删除不会自动删除本地已保存记录。
- 图片和文件消息显示附件信息或本地路径，不等于已经解析附件正文。

## 消息很多时的性能

首次建立全文索引和会话统计缓存需要一些时间；后续会使用增量采集、解密基线复用和索引查询。三个及以上字符的正文查询使用 FTS，两个字符的查询自动走兼容回退。后台 PID 和心跳在 `logs/collector.pid`，运行日志在 `logs/collector.log` 和 `data/collector.stdout.log` / `data/collector.stderr.log`。

## 常见问题

### 找不到 Python

安装 64 位 Python 3.9+，并确认 `python` 或 `py -3` 可以启动。程序不会联网替你下载 Python。

### 找不到企微数据目录

先确认企微已登录并同步。仍失败时，可以显式指定配置：

```powershell
& ".\01_一键部署.cmd" -ConfigPath "D:\path\to\config.json"
```

### 密钥校验失败

保持企微登录并重新运行一次 BAT。程序会在本地重新提取和校验，不需要退出账号。

### Qoder 或 WorkBuddy 连不上

先检查 `mcp_config.generated.json` 中的路径是否存在，再运行：

```powershell
& ".\.venv\Scripts\python.exe" ".\skills\wecom-local-mcp\scripts\verify_agent_deploy.py" --require-data
```

### 消息出现乱码或奇怪前缀

使用 v1.2.3 或更高版本，并重新运行一次 BAT 更新本地消息。旧版本已经处理过 protobuf 前缀、图片乱码和单字消息退化问题。

### 移动解压目录后失效

在新目录重新运行 BAT，并重新导入生成的 MCP 配置，因为配置里使用的是绝对路径。

## 给 Agent 自己使用

仓库附带 [Agent Skill](skills/wecom-local-mcp/SKILL.md)，其中包含离线部署、Qoder/WorkBuddy 注册、协议验证和排错步骤。

## 技术资料

- [实现、消息格式与验证记录](TECHNICAL.md)
- [第三方说明](THIRD_PARTY_NOTICES.md)
- [全部发布版本](https://github.com/sutanna233/wecom-local-mcp/releases)
