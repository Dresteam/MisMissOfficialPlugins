# 直播间信息

发送 **`直播间`**（或别名 `直播数据`），机器人输出当前直播间的实时数据卡片。

```text
@触发人
直播间数据：
🎤分区：情感-闲聊
⏰直播时长：01:51:13
👪累计人数：38
💗关注数：576
☀️热度：2146
👩超粉：0
🍁贵宾：1
🏅️小时榜：暂无
💰小时榜差值/距离上榜：832
📛勋章：z野玫瑰
```

## 权限

默认**仅房管 / 主播**可用，其他人发送指令会被静默忽略。

在插件配置里可以：

- 关闭 `admin_only`：所有人都能查；
- 在 `allowed_users` 里追加用户名或用户 ID；
- 设置 `deny_reply`：无权限时回一句提示（留空则静默）。

## 数据来源

全部走猫耳公开接口，不需要登录态：

| 字段 | 来源 |
|------|------|
| 分区 | `GET /api/v2/meta/data`（`catalog_id` → 一级-二级分区名） |
| 直播时长 | `GET /api/v2/live/{room_id}` 的 `status.open_time` |
| 累计人数 / 在线人数 / 关注数 / 热度 | 同上，`statistics.accumulation / online / attention_count / score` |
| 贵宾 | `GET /api/v2/chatroom/vip/list?room_id=` 的 `vip_num` |
| 超粉 | `GET /api/v2/chatroom/superfan/rank?room_id=` 的 `pagination.count` |
| 小时榜 / 差值 | `GET /api/v2/chatroom/meta?room_id=` 的 `hour_rank.rank / rank_up` |
| 勋章 | 同上房间信息里的 `medal.name` |

猫耳公开接口**没有提供**分区排名、热度排名、Banner 位、推荐位、福星榜、
PK 胜率，这些项**直接不输出**，卡片只保留能拿到真实数据的字段。

`show_unknown` 只影响「接口临时失败 / 该房间确实没有这项数据」时是否显示
「暂无」行（例如未上榜时的小时榜），不会让不支持的项重新出现。

## 配置项

| 配置 | 默认 | 说明 |
|------|------|------|
| `cmd` | `直播间` | 触发指令（精确匹配） |
| `cmd_aliases` | `["直播数据"]` | 别名列表 |
| `admin_only` | `true` | 仅房管 / 主播可用 |
| `allowed_users` | `[]` | 额外允许的用户名或用户 ID |
| `deny_reply` | `""` | 无权限时的回复，留空静默 |
| `cooldown_seconds` | `5` | 同一直播间两次查询的最短间隔 |
| `cache_seconds` | `10` | 数据缓存时长 |
| `max_chars` | `180` | 单条消息上限，超出按行拆分 |
| `show_unknown` | `true` | 是否显示「暂无」行 |
| `mention` | `true` | 首行是否 @ 触发人 |

## 实现说明

- 请求统一走 `HTTPClient` 的 `BACKGROUND` 车道，享受全进程的出站节流与在途并发上限；
- 分区表缓存 1 小时，房间快照缓存 `cache_seconds` 秒；
- 任一接口失败只影响对应字段（显示「暂无」），不会让指令整体报错；
- 同一直播间有 `cooldown_seconds` 冷却，避免被连续刷指令打满平台接口。
