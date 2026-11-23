# 客户反馈 5 条 · 源码定位与改法（0.7.6 → 0.7.7/0.7.8）

> 输入：客户 2026-11-23 的 `给桥接厂家的反馈.md` + 现场 `bridge-pipeline.log{,.1,.2}`（1049 条 event，已全量核对）。
> 结论：5 条全部能在本方源码定位。**3 条是实现选择，2 条是能力已存在但没暴露/没接线。**

## 一、先对齐事实（日志核过的数）

| 客户说法 | 实测 | 对应代码 |
|---|---|---|
| 实时通道抓不到，76% 靠回扫 | pdd_cdp 253 条，history 796 条 = 75.9% | `pddbridge_source.py:_health_check` |
| 实时事件 0–1 秒 | 253 条：-1.67 ~ +1.31s | — |
| 回扫固定 88–90 秒 | 新消息 179 条：**88.16–90.65s**，与抓取时刻无关 | `message_timing.py:history_ready` |
| 改 30s 轮询没用 | pull=120s 时 88.18–90.59；pull=30s 时 88.16–90.65 | 同上（门槛相对平台 ts） |
| 上传本身很快 | 队列等待中位 0.25s | `agent.py:_flush_events` |
| socket state=none | 232 条警告，从头到尾没有一次 connected | `pddbridge_source.py:867` |
| 15 账号 → 6 → 2 | 18:13:48=15 → 18:25:48=6 → 18:26:21=2 | CDP eval 8s 超时连锁 |
| 40 会话上限 | `history_pull_max_buyers` 默认 40 | `agent.py:1256` |
| 30 分钟丢弃 | 延迟分布铺到 29.8 分钟**直接归零** | `history_report_max_age_seconds=1800` |

客户没提但更严重的一条：796 条回扫里 **621 条（78%）抓取延迟 >60 秒**，中位 559 秒，p90 1431 秒。原因见问题 3。

---

## 二、问题 1：实时通道 —— 注入层挂错层了（P0，收益 90s → 1s）

### 根因
注入脚本挂的是页面的 `window.WebSocket` / `window.socketUtil`（`_inject_payload`）。
PDD 3.5.0.40 的 IM 在原生 C++（`window.pddWindows.jsInvokeCPlusPlus`），页面里根本没有 WebSocket 实例，
所以 `_health_check` 里 `su.socket.readyState == null` → 永远 `state=none`（`pddbridge_source.py:867-880`）。
同一段代码里还留着 `sendTextReady` 判断，也说明这一层只是页面壳。

### 我们已经有的正确通道（不用重写）
`recv_via_dll` → `bridge/pdd_recv.py` → DLL `wb_recv.h`：inline hook `PddWorkbench.exe+0x37544`（**3.5.0.40 版本专属 RVA**，即探域接收主路）。
证据：`D:\temp\pdd-send-hook\out\pdd_recv_spool.log` 里已是真实买家 push 帧（`push_type:2`、`from.role=user`、`msg_id`、`ts` 齐全），零延迟。

### 但接线是断的，四处必须改
1. **帧格式**：DLL 发 `FRAME|<seq>|<json>`，`pdd_recv.parse_recv_line` 只切 `FRAME|`
   → 交出 `<seq>|{...}` → `protocol.parse_frame` 判 `unparsable`，全部落诊断帧。
   改为 split 出 seq，返回 `(seq, raw)`。
2. **没有回 ACK**：DLL 侧 `pipe_read_acks` 等 `ACK|<seq>`（`pdd_send_v5.cpp:1410-1440`），
   Python 从不回 → spool 永不确认、断线后整段重放。`_pump` 里收到帧就回写 ACK。
3. **载荷形状不同**：工作台 push 是
   `{"push_type":2,"push_data":{"seq_id":..,"data":[{"message":{from,to,content,msg_id,ts}}]},"target_id":..}`，
   而 `parse_frame` 认 `{"cmd":"push","message":{...}}`。
   需要一层适配：`push_type==2` → 取 `data[0].message`、补 `cmd:"push"`、把 `target_id` 映射成 `mall_cs` 端点
   （后续 `seller_id()` / `buyer_uid()` 已能处理，`protocol.py:96-160`）。
4. **默认值指向错 DLL**：`pdd_dll_path` 默认 `pdd_send_v3.dll`，而 RECV/PROBE 只在 **v5**；
   `recv_via_dll` 默认 false、`recv_slot` 默认 -1（`config.py`）。
   首次上线走探针：`PROBE|<log>` → `pdd_recv_probe.log` 确认接收槽 → 写 `recv_slot`。

> 顺带：`_health_check` 的探测本身每 5s 对同一 CDP 连接做一次 eval，和 pump 抢连接，
> 是 15 账号时连锁 `cdp eval 无响应` 的放大器（见问题 4）。

---

## 三、问题 2：88–90 秒不是 bug，是给"老中心"留的兼容门槛（但必须可配）

### 根因
```python
# bridge/message_timing.py:6-7,68-73
LIVE_MAX_AGE_SECONDS = 90.0
def history_ready(event, now):
    """Older centers ignore history flags: never submit a replay inside their live window."""
    return not event.get("is_history") or now - timestamp > LIVE_MAX_AGE_SECONDS
```
即：history 事件必须等平台时间**老于 90 秒**才允许上送，防止"不认 is_history 的老中心"把回补当实时消息、误触发回复。

### 改法（两级，先安全后彻底）
1. 加配置 `history_live_window_seconds`（默认 90；`0` = 立即上送），`history_ready` 读它；
   `_flush_events` 里 history 的 20% 配额（`agent.py:751`）随门槛放开重新分配（否则会挤占实时）。
2. 按中心能力开关 `history_hold_policy = legacy | ack_capable`：
   我们自己的中心在 `CENTER_CONTRACT_REQUEST_0.5.19.13.md` 里已明确历史消息返回 `ignored`（认 `is_history`），
   确认后直接上送，88 秒归零；老中心继续 90s。

---

## 四、问题 3：40 是个默认值，真正的病是"候选表 + 串行拉取"

`history_pull_max_buyers` 其实早就能配（默认 40，上限 500）。但三个实现问题叠加：

1. **候选表几乎不更新**：`_recent_buyers` 只由实时帧写入
   （`agent.py:347`：`not msg.get("is_history")`）。实时通道一死，活跃表就冻住了，
   历史会话永远进不了候选 → 只能靠 CDP 偶尔漏出的实时帧。
2. **每轮永远取最近活跃的前 N**（`agent.py:1264` `sorted(...)[:max_buyers]`），没有游标，
   排后面的会话永远轮不到。
3. **串行拉取**：`for` 循环里逐个 `pull_history(..., timeout=5.0)` + `gap 0.3s`（`agent.py:1273-1276`），
   一轮 = N×(eval + 0.3s)；40 个会话轻松超过 30s 周期，遇到慢会话单轮可到 200s。
   叠加上 1800s 上限，就是现场那 78% >60s、p90 1431s、到 29.8 分钟直接丢。

### 改法
- 候选 = 最近活跃 ∪ 有未读会话，**落盘持久化**（重启不丢）+ `next_pull_at` 冷却字段；
- **真轮转游标**：每轮从上次位置继续，不在 top-N 里打转；
- **并发 2–4 路**拉取，`timeout` 降到 3s，每店独立 CDP 连接，避免互相阻塞；
- 指标：`targets_planned/attempted/skipped`、`round_seconds`、`cursor_lag`、`pulled_oldest_age`；
- `history_pull_max_buyers` 默认提到 200，或启动时按在线店铺数自动放宽。

---

## 五、问题 4：cdp eval 8 秒 + 无并发上限

- `bridge/pddbridge/cdp.py:114,125,163`：三处 `timeout=8` 硬编码。
- `_health_check` 每 `health_interval`(5s) 对每个 session 做一次 eval，与 pump 共用同一连接。
- 现场：18:24–18:26 共 14 条 `cdp drain error`（`cdp eval 无响应` ×12、`WinError 10053` ×2），
  同期在线账号 15→14→12→6→2，2 分钟内整片掉线。

### 改法
- 加 `cdp_eval_timeout_seconds`（默认 3.0）与 `cdp_health_interval_seconds`（默认 30）；
- 健康探测与数据 pump 分离（独立连接或同一把锁串行），避免探测饿死 pump；
- 单店连接数上限 = 账号数，超过排队而不是并发抢；
- eval 超时统一走 `cdp drain error -> 重扫`，但重扫要单店级，不要整片 `_close_sessions()`。

---

## 六、问题 5：队列卡住不打原因

`_flush_events` 只有凑出 batch 才打 `event upload start ... oldest_wait`；
`pending>0` 但 `ready==0`（即在等 88/90 秒门槛）时**一条日志都没有**，客服从时间数字上看不出是等门槛还是等中心。

### 改法（heartbeat 里加一行）
```
event queue hold pending=12 history=12 inflight=0 reason=hold_window
  min_remaining=41.2s oldest_ts_age=48.8s
```
`reason ∈ hold_window | inflight | client_retry | blocked_local_first | center_rejected`；
`oldest_wait > 30s` 时升 WARN，并把 `min_remaining` 打出来。这个改动几乎零风险，但最能止血客诉。

---

## 七、版本与配置迁移（别忘）

`load_config` 用的是 `data.get(key) or default`，**老配置里键存在就永远拿不到新默认**
（`config.py` 顶部注释已经踩过这个坑）。
所以本轮必须 `CONFIG_VERSION 1 → 2`，在 `_MIGRATIONS_V2` 里写入：
`cdp_eval_timeout_seconds`、`cdp_health_interval_seconds`、`history_live_window_seconds`、
`history_hold_policy`、`history_pull_max_buyers`、`recv_via_dll`、`recv_slot`、`pdd_dll_path`(→v5)。

---

## 八、建议拆版

| 版本 | 内容 | 风险 |
|---|---|---|
| **0.7.7** | 问题 3（游标+并发+候选表）、问题 4（超时/探测分离）、问题 5（hold 原因日志）、问题 2 的配置项 | 低，纯客户端，可灰度单店 |
| **0.7.8** | 问题 1 原生接收（帧适配 + ACK + v5 DLL + 探针定槽） | 中，需现场 `PROBE` 定 `recv_slot`，并做 3.5.0.40 RVA 白名单校验 |

问题 1 落地后，问题 2 对新消息自动失效（实时 0–1 秒），只剩回补消息走门槛 —— 所以 0.7.7 先把问题 2 做成配置，
0.7.8 上线后再评估是否默认 `history_hold_policy=ack_capable`。

---

# 附录 A：0.7.7 第一批已改（原生接收接线）

> 目标：让 `recv_via_dll` 这条路真的能通，且默认关闭不影响现网。

## A.1 改了什么

| 文件 | 改动 |
|---|---|
| `bridge/pdd_recv.py` | 按 DLL **v4 管道协议**重写：管道名 `pdd_recv_bridge_v4<sfx>` / `pdd_recv_ack_v4<sfx>`；`FRAME\|<seq>\|<json>` 解析出 seq；每帧处理成功才回 `ACK\|<seq>`（失败不回，等 DLL 重发）；兼容老格式 `FRAME\|<json>`；增加 `acked/ack_errors/connected_once` 指标与"管道不存在"告警 |
| `bridge/pddbridge/protocol.py` | 新增 `native_push_messages()`：把工作台原生信封 `{"push_type":2,"push_data":{"data":[{"message":…}]}}` 逐条拆成页面同构的 `{"cmd":"push","message":…,"target_id":…}`；已是页面帧形状的原样放行 |
| `bridge/pddbridge_source.py` | `feed_native_frame()` 走上面的适配层；一次推送里的多条消息逐条进管线，认不出的仍按诊断帧上报（不静默丢） |
| `bridge/config.py` | 新增 `recv_pipe_suffix` / `recv_pipe_name` / `recv_ack_pipe_name`（load + save + example） |
| `tests/test_pdd_native_recv.py` | 8 条新用例：seq 解析、管道名与后缀、信封拆包、端到端产出买家消息、ACK 语义 |
| `tools/replay_native_spool.py` | 用真实 spool 回放验证；`F\t<seq>\t<len>\t<json>` |

## A.2 验证结果

用现场机 `D:\temp\pdd-send-hook\out\pdd_recv_spool.log` 的真实帧回放：

```
spool 帧数        : 17
拆出的 push 条数  : {"push": 25}
产出买家消息      : 25
丢弃计数          : {}
  - 1789769315558 buyer=3563084726842 account=cs_427302374:164945148 ts=1789769315559 '怎么操作'
  - 1789769425736 buyer=3563084726842 account=cs_427302374:164945148 ts=1789769425 '怎么操作'
  - 1789770758307 buyer=3563084726842 account=cs_427302374:164945148 ts=1789770758 '亲，您发的截图…'
```

17 帧拆出 25 条消息、零丢弃、account 由 `target_id + mall_cs 端点` 正确推出 `cs_<mall>:<target>`、平台 ts 原样保留。
单测 12/12 通过（含 4 条既有用例未回归）。

## A.3 DLL 侧还差的四件事（客户端不改就还是不通）

1. **DLL 版本**：接收管道只在 **v5** 里（`pdd_send_v3.dll` 无 `recv_ack`）。`pdd_dll_path` 必须指向 `pdd_send_v5.dll`。
2. **注入器**：`injector.exe` 需要管理员（PddWorkbench 是 High IL）；工作台重启后要重新注入 → 安装包加守护。
3. **引擎选择**：`recv_slot` 走 vtable（`CMChatImpl::Register(VIMsgCallBack)`）是稳的那条；
   `0x37544` inline hook 在你们自己的 `out/pdd_wb.ini` 里标着"RVA 表已被撤回，需 mm 内实测序言"，**不要默认启用**。
4. **管道后缀一致性**：DLL 读工作台进程的 `PDD_BRIDGE_PIPE_SUFFIX`，桥接读 `recv_pipe_suffix`；
   一台机器多安装时必须显式配置同一个值，否则互相抢管道。

## A.4 现场启用步骤（灰度）

```jsonc
// bridge_config.json
{
  "recv_via_dll": true,
  "recv_slot": -1,                       // 先留 -1：自动进 PROBE 探针模式
  "pdd_dll_path": "D:\...\pdd_send_v5.dll",
  "recv_pipe_suffix": ""                 // 单安装留空
}
```

1. 注入 v5 → 桥接启动 → 日志出现 `原生接收探针已开启: …pdd_recv_probe.log`；
2. 让买家发一条消息 → 看探针日志里哪个槽位记到了买家帧 → 把 `recv_slot` 填成该槽；
3. 重启桥接 → 出现 `原生接收通道已启动（slot=N）`，`bridge-pipeline.log` 里应立刻出现 `source=…` 的实时事件；
4. 抓 20 店同时在线：`cdp_poll_interval` 可提到 1.0–2.0（推送已直连，drain 只兜底），健康探测降到 30s。

## A.5 与后续批次的边界

- 本批**没有**改 CDP 路径、没有改 90 秒门槛、没有动上传批次；`recv_via_dll=false` 时行为与 0.7.6 完全一致。
- 问题 3/4/5（逐店隔离、超时、hold 原因日志）在下一批，和本批互不依赖。

---

# 附录 B：0.7.8.0（回应客户 0.7.7.0 复测第 1、2 条）

客户 0.7.7.0 复测：出站 0 秒、入站仍 89–90 秒。**这是预期**——0.7.7.0 只做了原生通道
"接线"，`recv_via_dll` 默认 false（要现场 PROBE 定槽位），90 秒门槛一行没动。

## B.1 已改

| 文件 | 改动 |
|---|---|
| `bridge/message_timing.py` | `history_ready(event, now, window_seconds=None)`：门槛可传参；`hold_remaining()` 给"卡在哪"的日志用 |
| `bridge/agent.py` | `_history_window()`：`history_hold_policy=legacy|ack_capable`、`history_live_window_seconds=N`；`_queue_hold()` 心跳打原因；回扫循环改成**按起始时刻排程**（不再"做完再等 interval"），`history_pull_timeout_seconds` 默认 3s |
| `bridge/config.py` | 新增 4 个键并**写进 save_config**（老实现漏了 history_* 的持久化，GUI 一保存就回默认） |
| `tests/` | 新增 2 条：门槛语义（0/N/None）、policy→window 映射 |

## B.2 客户两条请求的落点

1. **"90 秒做成配置项，或降到 10–15 秒"**
   `history_hold_policy=ack_capable` → 立即上送；或 `history_live_window_seconds=15` → 等 15 秒。
   我们中心已按 `is_history` 返回 `ignored`（见 CENTER_CONTRACT_REQUEST_0.5.19.13.md），
   可以直接用 `ack_capable`；只有对接**第三方老中心**时才需要 `legacy`。
2. **"入站为什么只对当前打开会话生效，能否按账号订阅全部会话"**
   这就是 CDP 方案的物理限制：工作台只把"当前激活会话"的 push 交给页面，注入层只能看到这一条。
   按账号订阅全部会话 = 走原生进程级回调（`recv_via_dll`，0.7.7.0 已接线），
   它不是"页面订阅"，是工作台进程内 `CMChatImpl::Register(VIMsgCallBack)` 的全局回调，
   所有店铺、所有会话的 push 都会进来。

## B.3 现场建议配置（20 店）

```jsonc
{
  "recv_via_dll": true,              // 定槽后打开：入站实时，覆盖全部会话
  "recv_slot": N,                    // PROBE 探针确认
  "history_hold_policy": "ack_capable",
  "history_pull_seconds": 15,
  "history_pull_timeout_seconds": 3,
  "history_pull_max_buyers": 200,    // 20 店时 40 不够
  "history_pull_gap_seconds": 0.1
}
```

预期：开机/切店后 `history pull` 周期回到 15 秒量级、抓取延迟 ≤15 秒、上报门槛 0 秒 →
端到端 15–20 秒；`recv_via_dll` 生效后新消息直接 0–1 秒。
