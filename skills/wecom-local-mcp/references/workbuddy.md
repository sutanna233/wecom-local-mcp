# WorkBuddy 接入指南

官方依据：[Tencent WorkBuddy MCP Guide](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)。官方文档确认了可视化配置入口、用户级/项目级 `mcp.json` 路径，以及绿色/红色连接状态。

## 1. 准备本地配置

先通过包根目录的 `01_一键部署.cmd` 完成部署，再运行 Skill 的 `verify_agent_deploy.py`。正常接入直接复制本次生成的 `mcp_config.generated.json`，不从 MCP 市场安装其他服务。

可复制模板见 [workbuddy.mcp.json](workbuddy.mcp.json)。模板仅作结构对照或宿主预览；正常接入直接复制生成文件。生成文件缺失表示部署尚未完成，应重跑唯一入口，不用模板手工猜测路径。

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

实际生成配置若还有 `WECOM_CONFIG`，原样保留。不要向 JSON 添加密钥、数据库或消息正文。

## 2. 选择配置级别并导入

官方文档列出两种级别：

| 级别 | 配置位置 | 用途 |
|---|---|---|
| 用户级 | `~/.workbuddy/mcp.json` | 当前用户的各项目复用 |
| 项目级 | `<项目目录>/.workbuddy/mcp.json` | 仅当前项目生效 |

优先让 WorkBuddy UI 管理这些文件：

1. 在侧边栏进入 **插件**。
2. 点击右上角 **MCP 服务器** → **配置 MCP**。
3. 在编辑器中粘贴 `mcp_config.generated.json` 的完整内容。
4. 若已有其他 MCP 服务，保留原内容，只把 `wecom-local-messages` 合并进 `mcpServers`，不要整文件覆盖。
5. 保存，返回 MCP Server 状态页面。

Windows 下 `~` 表示当前用户目录。虽然上述路径来自官方文档，不同 WorkBuddy 版本的界面文案或默认级别仍可能变化；以当前 UI 显示的用户级/项目级选择为准。无法写入时由用户在 UI 中粘贴，不自行改 ACL 或改写到其他宿主配置。

## 3. 验证

1. 保存后查看 `wecom-local-messages` 的状态：官方文档以 **绿色**表示连接成功，**红色**表示配置或启动异常。
2. 运行本地 `verify_agent_deploy.py --require-data`，确认协议和五个只读工具均通过；空数据库可不加 `--require-data`，此时验证器会明确跳过 `get_message`。
3. 在 WorkBuddy 中让 Agent 调用 `list_conversations`，参数 `{"limit":1}`，只报告成功与否和分页元数据，不展示正文。
4. 再用用户指定的非敏感关键词调用 `search_messages`，并用返回的 `msgid` 调用 `get_message`；本地验证器同时确认未暴露 `sync_messages`。

如果 UI 为绿色但工具调用失败，分别检查宿主审批/DLP 与本地 MCP：宿主拦截由用户审查，本地协议问题用验证器定位，不关闭只读模式。

## 4. 常见问题

- **红色状态**：检查 JSON 括号/引号、三个绝对路径和 `.venv` 是否存在，再运行本地验证器。
- **已有服务消失**：恢复用户已有配置，只合并 `wecom-local-messages`，不要覆盖整个 `mcp.json`。
- **项目间行为不同**：检查用户级与项目级配置是否重复或指向不同包目录/数据库。
- **移动包目录后失败**：重新运行 `01_一键部署.cmd`，重新导入新生成的绝对路径。
- **消息为空**：确认本机企微已同步；OfflineDb 是静态副本，不会自动更新。
