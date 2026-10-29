# 企业微信消息助手（本地 MCP）

让 Qoder、WorkBuddy 或其他支持 MCP 的 Agent，直接搜索和读取你电脑里已经同步的企业微信消息。

**不用企业管理员权限，不用小号，不上传聊天记录。**第一次部署也不需要访问外网。

## 它能做什么

- 按关键词搜索消息，例如 `eh08`、`JPH`、文件名或某人说过的话。
- 查看某个群的最近消息，再按消息 ID 读取完整内容。
- 按会话、发送人和时间范围筛选。
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

Agent 会使用这些只读工具：

| 工具 | 用途 |
|---|---|
| `list_conversations` | 查找会话、群聊和联系人 |
| `search_messages` | 按正文、会话、发送人和时间搜索 |
| `recent_messages` | 查看最近消息 |
| `get_message` | 按 `msgid` 读取完整消息 |

## 如果你只有一份消息数据库

可以用同一个 BAT 部署离线查询，不需要启动企微：

```powershell
& ".\01_一键部署.cmd" -OfflineDb "D:\offline\wecom_messages.sqlite"
```

如果数据库旁边还有 `-wal` 文件，请一起保留。程序会在副本上完成 checkpoint 和完整性检查，不修改源文件。

## 数据与隐私

- 消息只在本机读取和保存，MCP 只走本地 stdio，不开放网络端口。
- 不要把 `wxwork_keys.json`、消息数据库或 `decrypted/` 上传给别人。
- 本工具只能读取**已经同步到本机**的消息，不能恢复服务器上从未同步、已删除或不可见的历史。
- 图片和文件消息显示附件信息或本地路径，不等于已经解析附件正文。
- 默认不启用持续采集；需要时运行 `02_启动消息采集.cmd`。

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
