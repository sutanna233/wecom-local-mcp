# Qoder 接入指南

本指南适用于 Qoder IDE/Connectors 的本地 STDIO MCP。Qoder CLI 有独立配置管理方式，不把 CLI 命令或路径套用到 IDE；CLI 用户请核对[官方 CLI MCP 文档](https://docs.qoder.com/cli/mcp-servers)。

官方依据：

- [Qoder MCP](https://docs.qoder.com/user-guide/chat/model-context-protocol)：STDIO 的 Name、Type、Command、Arguments，以及 My Servers 的工具列表和连接状态。
- [Qoder Connectors](https://docs.qoder.com/qoder/connectors)：通过 JSON 粘贴完整 MCP 配置并添加。

## 1. 准备本地配置

先在包根目录完成 `01_一键部署.cmd`，再运行 Skill 的 `verify_agent_deploy.py`。打开本次生成的 `mcp_config.generated.json`，它是唯一权威配置来源。

可复制模板见 [qoder.mcp.json](qoder.mcp.json)。模板仅作结构对照或宿主预览；正常接入直接复制生成文件。生成文件缺失表示部署尚未完成，应重跑唯一入口，不用模板手工猜测路径。

模板如下，三个 `C:\\path\\to\\...` 路径必须替换为目标机上的绝对路径；OfflineDb 的 `WECOM_MCP_DB` 通常指向 `data\\offline_messages.sqlite`：

```json
{
  "mcpServers": {
    "wecom-local-messages": {
      "command": "C:\\path\\to\\wecom-local-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\wecom-local-mcp\\wecom_mcp.py"
      ],
      "env": {
        "WECOM_MCP_DB": "C:\\path\\to\\wecom-local-mcp\\data\\wecom_messages.sqlite",
        "WECOM_MCP_READ_ONLY": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

实际生成配置若还有 `WECOM_CONFIG`，原样保留。不要把密钥、数据库或消息正文加入 JSON。

## 2. 导入或添加

优先使用当前 Qoder 版本的 JSON 入口：

1. 打开 Qoder 的 Connectors/MCP 页面，选择 **JSON**、**Import from JSON** 或当前版本对应的导入按钮。
2. 将 `mcp_config.generated.json` 的完整内容粘贴到 JSON 输入区。
3. 核对 `command`、`args` 和 `env` 后选择 **Add MCP** 或 **Import**；保存配置。
4. 若当前版本只显示表单，则选择/填写：
   - **Name**：`wecom-local-messages`
   - **Type**：`STDIO`
   - **Command**：生成配置中的 `command`
   - **Arguments**：生成配置 `args` 中唯一的 `wecom_mcp.py` 绝对路径
   - **Environment / env**：逐项填入生成配置的环境变量；若表单没有环境变量入口，改用该版本的 JSON 编辑/导入界面。

**版本差异与配置位置**：Qoder 官方流程通过 IDE 设置、Connectors 或 MCP 页面打开/粘贴配置。不同版本的入口名称和是否直接暴露 `mcp.json` 可能不同；本指南不臆测用户级或项目级磁盘路径。需要编辑文件时，从当前 UI 提供的 JSON 编辑或打开配置入口进入，不要按其他宿主或其他 Qoder 版本的路径新建文件。也不要覆盖已有 `mcpServers`，只合并 `wecom-local-messages` 节点。

无宿主配置写入权限时，由用户在 UI 中完成粘贴；不要修改 ACL 或改写其他配置文件。

## 3. 验证

1. 在 **My Servers** 或当前版本的服务列表中找到 `wecom-local-messages`。
2. 展开详情，确认连接成功（官方文档以链接图标表示）并出现：
   - `list_conversations`
   - `search_messages`
   - `recent_messages`
   - `get_message`
   - `count_messages`
3. 确认没有 `sync_messages`。首次工具调用若出现确认提示，由用户核对后批准，不为了省事开启所有后续工具的自动执行。
4. 做一次最小调用：让 Agent 使用 `list_conversations`，参数 `{"limit":1}`，只报告调用是否成功和分页元数据，不展示消息正文。
5. 再用用户指定的非敏感关键词调用 `search_messages`，从结果选 `msgid` 调用 `get_message`。也可仅运行本地验证器完成协议和五工具检查。

连接失败时先运行本地验证器并检查绝对路径、STDIO 类型和环境变量。不要点击可能联网下载依赖的 Quick Fix；本链路只允许随包 wheel 离线安装。

## 4. 常见问题

- **服务出现但工具缺失**：重新导入完整生成配置并刷新/重连；确认没有启用其他同名服务器。
- **命令启动失败**：检查 `.venv/Scripts/python.exe` 是否存在；移动包目录后重新运行唯一部署入口并重新导入。
- **工具返回数据库错误**：核对 `WECOM_MCP_DB`，OfflineDb 与在线模式的目标文件不同。
- **宿主审批拦截**：这是 Qoder 的调用确认，不是 MCP 协议故障；由用户审查并批准当前只读调用。
