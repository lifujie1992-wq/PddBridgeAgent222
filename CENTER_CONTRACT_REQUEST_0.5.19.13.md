# 桥接客户端 → 中心 接口调整请求

- 客户端版本：**0.5.19.13**（全部上报模式）
- 提出时间：2026-09-17
- 影响：客户消息能"上报成功"但进不了大脑；客户端只能落死信，无法自行修复

---

## 0. 背景（客户端这一轮改了什么）

按业务要求，客户端已改为「**全部上报，回不回由中心（大脑）决定**」：

- 除「按平台 `msg_id` 去重」（防同一帧被客户端 native + WebSocket 双通道投递两次）以外，客户端**不再按内容丢弃任何消息**；
- 历史消息、取不到买家 uid 的消息、席位 uid 当买家的消息、系统/协商帧、未知帧，**全部上报**，并带 `skipped_reason` / `is_diagnostic` 标记；
- 中心拒收的，客户端落**死信文件 + 原始帧归档**（不丢、可见、可补推），但业务上仍然没进大脑。

上线后实测：中心侧有两处规则把"已上报"的消息挡回，见下。

---

## 1. 请求一（最要紧）：`invalid_message` 不要拒收纯数字内容

### 现象

买家发的**纯数字内容**被中心以 `refused_reason = invalid_message` 拒收。截至报告时间共 4 条，例如：

| 内容 | 长度 | 平台时间 | 买家 | 店铺账号 |
|---|---|---|---|---|
| `98643187513451678` | 17 位 | 2026-09-17 01:38:12 | 4764375385604 | cs_427302374:164945148 |
| `316767548` | 9 位 | 2026-09-17 17:22:46 | 4764375385604 | cs_427302374:164945148 |

### 影响

这两条几乎肯定是**订单号 / 物流单号**。客户把单号发过来问物流，是**最需要 AI 接住**的一类消息，现在被当"uid 噪声"丢掉；客户端已经在 0.5.19.x 里确认过：这类内容是 `identity_kind=message_id`、`buyer_id` 有效的**正常买家消息**，不是协议噪声。

### 期望改法（任选其一）

1. **放行纯数字内容**：6~20 位纯数字不再按 `invalid_message` 拒收，正常接收（可加 `flags: ["content_digits"]`）。
2. **改为「接收 + 打标」**：`status=accepted, committed=true`，把可疑标记放进事件字段，是否回复交给大脑。
3. 若必须保留过滤，请**收窄判定**（例如只在"与任何已知会话/uid 都无关 + 长度异常"时才拒），并在 ack 里给出可区分 code，便于客户端补推。

### 期望响应示例

```json
{
  "ack_version": 1,
  "event_acks": [
    {"event_id": "b8188dcb244b…", "status": "accepted", "committed": true,
     "retryable": false, "flags": ["content_digits"]}
  ]
}
```

---

## 2. 请求二：诊断帧请接受，或明确回 `ignored`，不要 `unsupported_role`

### 现象

客户端把**非买家帧**（系统/协商帧、未知帧、空帧）也上报：`role = "platform"`、`buyer_id = ""`、`is_diagnostic = true`、`skipped_reason = "system_frame" | "unknown_kind" | "empty_frame"`、`frame_kind` 标明原始类型。

中心入参校验回 `refused_reason = unsupported_role`（拒收）。截至报告时间 7 条，全部是 `mall_system_msg`。

### 影响

- 这些帧进不了中心：客户端本地有归档，但中心看不到，**无法用来发现客户端协议漂移**（例如 PDD 改了帧格式、出现新的二进制帧）；
- 客户端把这 7 条落进死信文件，长此以往与真正的业务异常混在一起。

### 期望改法（任选其一）

1. **接受** `is_diagnostic = true` 的事件并归档，不回给大脑、不触发回复；
2. 或回 **`status: "ignored"` + `reason: "diagnostic"`**（语义"收到了但不用"），客户端就不会按异常处理；
3. 若不希望它们出现在 `/events` 里，请**给一个独立诊断端点**（如 `/api/bridge/v1/diagnostics`），客户端改推那里。

---

## 3. 事件字段契约（客户端当前会上报什么）

| 事件类型 | `role` | `buyer_id` | 关键字段 | 期望中心行为 |
|---|---|---|---|---|
| 买家消息 | `user` | 有值 | `identity_kind=message_id` | 正常走大脑 |
| 买家消息（带标记） | `user` | 有值 | `skipped_reason=no_buyer` | 建议接收留档；无买家标识时可不回复 |
| 买家消息（席位一端） | `user` | 席位 uid | `skipped_reason=seat_uid_as_buyer`, `is_diagnostic=true` | 建议**接收留档但不触发回复**（否则可能出现"AI 自问自答"） |
| 历史消息 | `user`/`mall_cs` | 有值 | `source=pdd_cdp_history`, `is_history=true` | 现在是 `ignored`，保持即可 |
| 我方发送回执 | `mall_cs` | 有值 | `source=pdd_cdp` | 现在是 `ignored`，保持即可 |
| 诊断帧 | `platform` | `""` | `is_diagnostic=true`, `skipped_reason`, `frame_kind`, `identity_kind=frame_digest` | 见 §2 |

补充字段：`skipped_reason`、`is_diagnostic`、`frame_kind`、`source`、`identity_kind`、`platform_message_id`。诊断帧的 `platform_message_id` 形如 `frame-<digest>`，与买家消息的 `message_id` 不会冲突。

---

## 4. 客户端现状（不依赖中心改动就不会丢数据）

| 机制 | 位置 | 说明 |
|---|---|---|
| 死信文件 | `bridge_queue_pdd_refused.jsonl` | 中心拒收的事件带 `refused_reason` 原样留档 |
| 原始帧归档 | `bridge_queue_pdd_frames_raw.jsonl` | 异常/被拒帧原样留档，20 MiB 轮转一份 |
| 开关 | `bridge_config.json` | `report_all`（默认 `true`）、`dedup_mode`（`platform_id` / `off`） |
| 计数 | 心跳 `status.reported` / `status.drops` | 照报计数 / 真丢弃计数分开暴露 |

中心改完 §1、§2 之后，客户端可加「死信一键补推」，把这段时间积压的事件重投进大脑（当前仅留档，不自动重投）。

---

## 5. 验收标准

1. 内容为纯数字的买家消息 → 中心返回 `accepted/committed`（或 `accepted` + `flags`），死信文件**不再增长**；
2. 客户端上报一帧 `is_diagnostic=true` → 返回 `accepted`，或 `ignored` 且 `reason` 可区分（不再是 `unsupported_role`）；
3. 客户端界面不因上述两类响应出现错误提示（0.5.19.13 已把 `ignored` / `invalid_message` / 诊断类拒收排除在"客户端故障"之外，改完后该逻辑可进一步简化）。

---

## 附录：原始样本（取自现网死信文件）

```json
{"event_id":"4ea59f14c94cdbdbdad55c2f2a8f1d23","type":"message","msg_id":"1789580292241","platform_message_id":"1789580292241","identity_kind":"message_id","buyer_id":"4764375385604","role":"user","content":"98643187513451678","ts":1789580292,"account":"cs_427302374:164945148","source":"pdd_cdp","refused_reason":"invalid_message"}
{"event_id":"b8188dcb244b…","type":"message","msg_id":"1789636966238","platform_message_id":"1789636966238","identity_kind":"message_id","buyer_id":"4764375385604","role":"user","content":"316767548","ts":1789636966,"account":"cs_427302374:164945148","source":"pdd_cdp","refused_reason":"invalid_message"}
{"event_id":"39c29e8643b1…","type":"message","msg_id":"frame-ad9de76e426a0488378e","platform_message_id":"frame-ad9de76e426a0488378e","identity_kind":"frame_digest","buyer_id":"","role":"platform","content":"{ \"message\" : { \"data\" : { \"min_supported_msg_id\" …","ts":1789636862,"account":"cs_427302374:164945148","source":"pdd_cdp_diagnostic","skipped_reason":"system_frame","is_diagnostic":true,"frame_kind":"mall_system_msg","refused_reason":"unsupported_role"}
```
