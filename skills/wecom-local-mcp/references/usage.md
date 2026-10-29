# 本地消息工具使用参考

所有工具只查询生成配置中 `WECOM_MCP_DB` 指向的本地 SQLite。以下 JSON 是宿主 `tools/call` 的 `arguments`，不是需要发送到网络的请求。

## list_conversations

用于找到会话显示名、`conversation_id`、类型、消息数和最近时间。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `query` | string | 可省略 | 按会话显示名或 ID 做子串过滤 |
| `limit` | integer | 50，1–200 | 本页最大条数 |
| `offset` | integer | 0，非负 | 分页偏移 |

```json
{"query":"项目","limit":20,"offset":0}
```

结果的消息数组字段为 `conversations`。后续把所需项的 `conversation_id` 传给搜索或最近消息工具。

## search_messages

按消息正文的字面量子串搜索，必传非空 `query`。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `query` | string | 必填 | 正文关键词；通配符不具有特殊意义 |
| `conversation_id` | string | 可省略 | 限定一个会话 |
| `sender` | string | 可省略 | 显示名子串，或发送人 ID 精确匹配 |
| `start_time` | integer/string | 可省略 | 起始时间，包含 |
| `end_time` | integer/string | 可省略 | 结束时间，不包含 |
| `limit` | integer | 20，1–100 | 本页最大条数 |
| `offset` | integer | 0，非负 | 分页偏移 |

按会话和发送人筛选：

```json
{
  "query":"上线",
  "conversation_id":"R:example",
  "sender":"张三",
  "limit":20,
  "offset":0
}
```

查询本地某个日期（结束日期包含当天）：

```json
{
  "query":"周报",
  "start_time":"2026-10-01",
  "end_time":"2026-10-01",
  "limit":20
}
```

查询带时区的半开区间：

```json
{
  "query":"发布",
  "start_time":"2026-10-01T00:00:00+08:00",
  "end_time":"2026-10-02T00:00:00+08:00"
}
```

时间可为 Unix 秒/毫秒或 ISO-8601。无偏移 ISO 和 date-only 按 MCP 进程所在机器的本地时区解释；带偏移 ISO 按偏移转换。内部始终使用 `msgtime >= start_time` 和 `msgtime < end_time`。

## recent_messages

不依赖关键词，按时间倒序读取本地最近消息。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `conversation_id` | string | 可省略 | 限定一个会话 |
| `limit` | integer | 20，1–100 | 本页最大条数 |
| `offset` | integer | 0，非负 | 分页偏移 |

```json
{"conversation_id":"R:example","limit":10,"offset":0}
```

## get_message

从 `search_messages` 或 `recent_messages` 的结果中取出 `msgid`，读取完整正文及解析字段。

```json
{"msgid":"wxwork:message_table:12345"}
```

`get_message` 可能返回 `raw` 中的额外元数据。只把当前任务所需字段交给上层，不转发无关原始字段。

## 分页与结果

`list_conversations` 的结果数组名为 `conversations`；其余查询的结果数组名为 `messages`。分页元数据语义一致：

- `count`：本页条数。
- `total`：当前过滤条件下的总命中数。
- `offset`：本页偏移。
- `has_more`：是否还有下一页。
- `next_offset`：下一页应使用的偏移；没有下一页时为 `null`。

示例：第一页使用 `{"limit":20,"offset":0}`，若返回 `has_more=true`、`next_offset=20`，下一页使用 `{"limit":20,"offset":20}`，其他过滤条件保持不变。

消息摘要中的 `msgtype` 是整数，`type_name` 是可读类型；`content_truncated=true` 表示摘要正文被截断，此时用 `msgid` 调用 `get_message`。

## 推荐查询顺序

```text
list_conversations（可选，定位会话）
  → search_messages 或 recent_messages（小 limit）
  → 根据 next_offset 分页（仅在需要时）
  → get_message（仅读取选中的 msgid）
```

空结果可能表示关键词、发送人、会话或时间范围不匹配，也可能表示消息从未同步到本机。不要据此推断服务器端不存在该消息。
