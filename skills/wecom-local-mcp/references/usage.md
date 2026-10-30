# 本地消息工具使用参考

所有工具只查询生成配置中 `WECOM_MCP_DB` 指向的本地 SQLite。以下 JSON 是宿主 `tools/call` 的 `arguments`，不是需要发送到网络的请求。默认只读工具为 `list_conversations`、`search_messages`、`recent_messages`、`count_messages`、`get_message`。

## 通用过滤语义

### 时间范围

`search_messages`、`recent_messages` 和 `count_messages` 支持：

| 参数 | 类型 | 说明 |
|---|---|---|
| `start_time` | integer/string | 起始时间，包含 |
| `end_time` | integer/string | 结束时间，不包含 |

时间可为 Unix 秒/毫秒或 ISO-8601。无偏移 ISO 和 date-only 按 MCP 进程所在机器的本地时区解释；带偏移 ISO 按偏移转换。内部使用 `msgtime >= start_time` 和 `msgtime < end_time`。

date-only 的 `end_time` 表示包含该本地日期整天，内部转换为次日午夜。因此，查询 2026-10-01 当天应写：

```json
{"start_time":"2026-10-01","end_time":"2026-10-01"}
```

查询带时区的半开区间：

```json
{
  "start_time":"2026-10-01T00:00:00+08:00",
  "end_time":"2026-10-02T00:00:00+08:00"
}
```

### flag 位过滤

`search_messages`、`recent_messages` 和 `count_messages` 支持 `flag_mask`、`flag_value`（均为整数），过滤公式为：

```text
(flag & flag_mask) == flag_value
```

- 提供 `flag_mask` 而省略 `flag_value` 时，`flag_value` 默认为 `0`，即筛选掩码覆盖位均为 0 的记录。
- 只提供 `flag_value`、不提供 `flag_mask` 会返回参数错误。
- 要筛选置位，必须同时给出掩码和值。例如筛选第 0 位为 1：`{"flag_mask":1,"flag_value":1}`。
- 筛选第 0 位为 0：`{"flag_mask":1}`。
- `flag` 的位含义随消息类型和客户端版本而变，不要自行把某一位直接等同于“已读”或“撤回”。

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

结果数组字段为 `conversations`。后续把所需项的 `conversation_id` 传给其他消息工具。

## search_messages

按消息正文的字面量子串搜索，也可不提供关键词、只使用结构化过滤。`query` 省略、为空或仅含空白时，不添加正文条件；其余有效条件按 AND 组合。为避免无目的地遍历全库，空查询时建议至少提供会话、发送人、时间或 `flag` 条件，并使用小 `limit`。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `query` | string | 可省略/留空 | 正文关键词；通配符不具有特殊意义 |
| `conversation_id` | string | 可省略 | 限定一个会话 |
| `sender` | string | 可省略 | 显示名子串，或发送人 ID 精确匹配 |
| `msgtype` | integer | 可省略 | 限定数值消息类型 |
| `start_time` | integer/string | 可省略 | 起始时间，包含 |
| `end_time` | integer/string | 可省略 | 结束时间，不包含 |
| `flag_mask` | integer | 可省略 | `flag` 位掩码 |
| `flag_value` | integer | 有掩码时默认 0 | 掩码过滤的期望值，不能单独提供 |
| `limit` | integer | 20，1–100 | 本页最大条数 |
| `offset` | integer | 0，非负 | 分页偏移 |

按正文、会话和发送人筛选：

```json
{
  "query":"上线",
  "conversation_id":"R:example",
  "sender":"张三",
  "limit":20,
  "offset":0
}
```

空查询：只筛选某个数字发送人在本地某一天的消息：

```json
{
  "query":"",
  "conversation_id":"R:example",
  "sender":"1001",
  "start_time":"2026-10-01",
  "end_time":"2026-10-01",
  "limit":20
}
```

空查询叠加 `flag` 置位过滤：

```json
{
  "conversation_id":"R:example",
  "flag_mask":5,
  "flag_value":5,
  "limit":20
}
```

`sender` 显示为数字通常表示本地缺少联系人/群成员名称映射，不是查询错误；此时仍可用显示名子串或发送人 ID 精确匹配。

## recent_messages

不依赖关键词，按时间倒序读取本地最近消息。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `conversation_id` | string | 可省略 | 限定一个会话 |
| `sender` | string | 可省略 | 显示名子串，或发送人 ID 精确匹配 |
| `msgtype` | integer | 可省略 | 限定数值消息类型 |
| `start_time` | integer/string | 可省略 | 起始时间，包含 |
| `end_time` | integer/string | 可省略 | 结束时间，不包含 |
| `flag_mask` | integer | 可省略 | `flag` 位掩码 |
| `flag_value` | integer | 有掩码时默认 0 | 掩码过滤的期望值，不能单独提供 |
| `limit` | integer | 20，1–100 | 本页最大条数 |
| `offset` | integer | 0，非负 | 分页偏移 |

读取一个会话在指定半开时间窗内、第 0 位置位的最近消息：

```json
{
  "conversation_id":"R:example",
  "start_time":"2026-10-01T00:00:00+08:00",
  "end_time":"2026-10-02T00:00:00+08:00",
  "flag_mask":1,
  "flag_value":1,
  "limit":10,
  "offset":0
}
```

## count_messages

对过滤后的消息计数并分组，不分页，不返回消息正文。它适合先统计分布，再用 `search_messages` 或 `recent_messages` 读取少量目标记录。

| 参数 | 类型 | 默认/限制 | 说明 |
|---|---|---|---|
| `group_by` | string/string[] | 默认 `conversation` | 可单选或多选 `conversation`、`sender`、`msgtype`、`day` |
| `query` | string | 可省略/留空 | 正文字面量子串；留空则不加正文条件 |
| `conversation_id` | string | 可省略 | 限定一个会话 |
| `sender` | string | 可省略 | 显示名子串，或发送人 ID 精确匹配 |
| `msgtype` | integer | 可省略 | 限定数值消息类型 |
| `start_time` | integer/string | 可省略 | 起始时间，包含 |
| `end_time` | integer/string | 可省略 | 结束时间，不包含 |
| `flag_mask` | integer | 可省略 | `flag` 位掩码 |
| `flag_value` | integer | 有掩码时默认 0 | 掩码过滤的期望值，不能单独提供 |

`group_by` 可写为字符串、逗号分隔字符串或字符串数组，以下两种等价：

```json
{"group_by":"conversation,sender"}
```

```json
{"group_by":["conversation","sender"]}
```

多维分组时，每个组行包含所有所选维度。按本地日期统计一个会话在 2026 年 10 月的消息：

```json
{
  "group_by":"day",
  "conversation_id":"R:example",
  "start_time":"2026-10-01",
  "end_time":"2026-10-31"
}
```

按会话和发送人进行多维统计：

```json
{
  "group_by":["conversation","sender"],
  "query":"周报",
  "start_time":"2026-10-01",
  "end_time":"2026-10-31"
}
```

返回结构：

```json
{
  "total":42,
  "count":42,
  "group_by":["day"],
  "groups":[
    {"day":"2026-10-01","count":40},
    {"day":"2026-10-02","count":2}
  ]
}
```

这里的 `total` 和 `count` 都是**过滤后的消息记录数**，不是 `groups` 数组长度，也不是去重后的组数。`groups` 中每行有自己的 `count`，并按维度包含：

| 分组维度 | 组行附加字段 |
|---|---|
| `conversation` | `conversation_id` |
| `sender` | `sender`、`sender_id` |
| `msgtype` | `msgtype`、`type_name` |
| `day` | `day`，格式 `YYYY-MM-DD`，使用 MCP 进程本地日期 |

## get_message

从 `search_messages` 或 `recent_messages` 的结果中取出 `msgid`，读取完整正文及解析字段。

```json
{"msgid":"wxwork:v2:s:R%3Aexample:12345"}
```

`get_message` 可能返回 `raw` 中的额外元数据。只把当前任务所需字段交给上层，不转发无关原始字段。

## 消息摘要字段

`search_messages`、`recent_messages` 和 `get_message` 的消息对象包含以下 v1.3 字段：

| 字段 | 含义 |
|---|---|
| `flag` | 原始整数位值，旧 schema 可从 `raw.flag` 兜底 |
| `flag_bits` | 从 0 开始的置位位置列表，例如 `flag=5` 时为 `[0,2]` |
| `at_user_id` | 被 @ 用户 ID 数组 |
| `at_user` | 被 @ 用户显示文本数组，通常每项带 `@` |
| `mentions` | 结构化数组，每项为 `{"user_id":...,"name":...}`；`name` 保留 `@` 显示形式 |
| `mention_all` | 是否从现有 ID、名称或正文线索识别为 @所有人 |
| `dedupe_key` | 重复组键，64 位十六进制 SHA-256 |
| `dup_count` | 全库中具有相同键的不同 `msgid` 数量，至少为 1 |

`dedupe_key` 的输入为 JSON 数组 `[conversation_id,msgtime,normalized_content]`：正文会先去掉首尾空白并把连续空白折叠为一个空格。相同键只表示“同会话、同秒、规范化正文相同”的重复线索；可能包含人工重发等真实独立记录。

`dup_count` 是全库同键记录数，不受当前查询过滤条件或分页影响。标记重复组不会删除记录，也不会减少 `search_messages.total`、`recent_messages.total` 或 `count_messages.count`。它不同于采集阶段使用稳定 `msgid` 消除跨消息表重复。

消息的 `msgtype` 为整数；`type_name` 优先采用 `raw.type_name`，否则使用本地类型表。`content_truncated=true` 表示摘要正文被截断，此时用 `msgid` 调用 `get_message`。附件记录、附件路径或文件名不等于附件正文。

## 分页与结果

`list_conversations` 的结果数组名为 `conversations`；`search_messages` 和 `recent_messages` 的结果数组名为 `messages`。分页元数据语义一致：

- `count`：本页条数。
- `total`：当前过滤条件下的总命中记录数。
- `offset`：本页偏移。
- `has_more`：是否还有下一页。
- `next_offset`：下一页应使用的偏移；没有下一页时为 `null`。

第一页使用 `{"limit":20,"offset":0}`，若返回 `has_more=true`、`next_offset=20`，下一页使用 `{"limit":20,"offset":20}`，其他过滤条件保持不变。`count_messages` 不使用这套分页元数据。

## 推荐查询顺序

```text
list_conversations（可选，定位会话）
  → count_messages（可选，先看数量分布）
  → search_messages 或 recent_messages（小 limit）
  → 根据 next_offset 分页（仅在需要时）
  → get_message（仅读取选中的 msgid）
```

空结果可能表示关键词、发送人、会话、消息类型、`flag` 或时间范围不匹配，也可能表示消息从未同步到本机。不要据此推断服务器端不存在该消息。源端物理删除不会自动删除本地记录；每轮采集全量重扫 O(N)，扫描数不等于新增数。

## 在线后台采集状态

在线模式通过 `01_一键部署.cmd` 部署后，首轮同步完成即默认在当前用户会话启动后台采集，不需要另开采集启动器。重复运行入口会检查 `logs/collector.pid` 中的 PID 是否仍对应存活进程，存活时复用，不重复启动；采集器自身持有 `logs/collector.lock`，因此异常残留的状态文件不会永久阻止重启。

| 路径 | 用途 |
|---|---|
| `logs/collector.pid` | JSON 状态：`pid`、Unix 秒 `started_at` 和随采集循环更新的 `heartbeat` |
| `logs/collector.lock` | 采集器生命周期互斥锁；不要把它当作普通 PID 文件编辑或删除 |
| `data/collector.stdout.log` | 部署启动的后台标准输出 |
| `data/collector.stderr.log` | 部署启动的后台标准错误 |

只执行部署和首轮同步时，通过同一入口禁用默认后台采集：

```powershell
& ".\01_一键部署.cmd" -NoWatcher
```

旧命令中的 `-StartWatcher` 继续兼容；不传它也会默认启动。`-NoWatcher` 优先于 `-StartWatcher`。OfflineDb 是静态只读副本，始终不启动后台采集；需要刷新时重新通过入口部署新的校验副本。
