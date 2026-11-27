# 90科技多多助手 1.0.12（基于 0.7.6 稳定版）

客户交付包，刻意不引入 0.8.x 的新链路，只做改名与默认值对齐。

## 相对 0.7.6 的改动

| 项 | 值 |
|----|----|
| 产品名 | 90科技拼多多桥接助手 → **90科技多多助手**（窗口标题、托盘、启动日志、安装包、快捷方式） |
| 版本 | 0.7.6.0 → **1.0.0** |
| 默认中心 | `http://47.107.138.228:18765` → **`http://47.112.219.14:18765`**（与现网工位一致） |
| 默认日志目录 | 写死 `D:\kefuAgent\探域\tanyu2.9.1\logs` → 自动发现 `C:\Users\Public\PddPortableRuntime\<版本>\plugins\logs` |
| 默认数据源 | `cdp`（页面无 JS socket 收不到实时消息）→ **`tanyu_logs`** |
| 安装目录 | `{localappdata}\Programs\DuoDuoHelper`（新 AppId，不与旧版互相覆盖） |
| 安装包 | `90科技多多助手-Setup-1.0.0.exe` |

发送通道仍是 0.7.6 的页面 CDP（`send_via_dll=False`），**不注入 DLL、不弹 UAC、安装不会关闭拼多多工作台**。

## 构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_1.0.0.ps1 `
  -Python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\90tech-duoduo-v1.0.0.iss
```

## 安装后确认

1. 开始菜单/桌面出现「90科技多多助手」。
2. `%LOCALAPPDATA%\Programs\DuoDuoHelper\logs\bridge-pipeline.log` 出现
   `90科技多多助手 1.0.0 启动  工位=...  服务器=http://47.112.219.14:18765`。
3. 同一日志出现 `已开始监听本机探域日志（拼多多）` 且路径是 `...\PddPortableRuntime\3.5.0.40\plugins\logs`。
4. 买家发一条消息 → 日志 `event queued ... source=plugin` → 中心下发指令 → `send done status=confirmed`。

## 已知边界

- 工作台聊天 socket 未连接（没点开任何买家会话）时，回执不会来，发送会等满 5 秒判 `indeterminate`；
  这是 0.7.6 的既有行为，交付说明里让客服「保持一个买家会话开着」。
- 同时只能跑一个安装：本包与旧版 `PddBridgeAgent` 的进程名相同，安装会按名字清理旧进程。

## 1.0.1 修复（相对 1.0.0）

**AI 已回复、买家也收到了，浮窗倒计时还在走。**
0.7.6 的本地会话只从插件日志收 `buyer_msg`，卖家方向没有任何来源，平台回显也不一定抓得到；
浮窗/工作台的倒计时靠「本地会话里有卖家回复」清零，所以永远清不掉。
现在发送成功后会主动补写一条 `role=mall_cs / source=bridge_self_send / delivery_status=confirmed`
到本机工作台（只推本地，不上传中心）。

另：浮窗（PddAdsorbWindow）改为先于网关启动 —— 升级后旧网关健康探测常要等几十秒，
浮窗跟着等会让人以为没装上。

## 1.0.2 修复（相对 1.0.1）

**工作台没点开买家会话时，发送要干等 5 秒且被报成「未确认」。**
PDD 的聊天 socket 断开时不会推 `send_message` 回执。现在：

- 明确探测到 socket 断开（`none/no_socket/probe_failed`）→ 只等 2 秒；
- 页面已受理（`__pddBridge_sendText` 返回 ok）→ 直接回报
  `status=accepted / real_send=true / echo_missing=true`，并在 `error_user` 里提示
  「工作台未连接买家会话，建议点开该买家会话」；
- 不再把「已经发出去的消息」回报成 `real_send=false`，中心不会误判为未发送。

实测（模拟页面 0.4s 受理、socket=none）：**2.00s / accepted**（改前 6.95s / indeterminate）。

浮窗倒计时仍然由 1.0.1 的「本地补写卖家回复」清零，与本条互不依赖。

## 1.0.12 修复（相对 1.0.2）—— 1.0.2 会丢消息

1.0.2 为了「socket 断开别干等」把等待砍到 2 秒，结果**排队中还没轮到执行的发送被撤销**，
消息直接丢掉（现场 04:10:20 那条 `1795637406820` 就是这么没的：日志里没有 `send accepted`，
3.05s 后报 indeterminate）。

现在拆成三步：

1. 先等它**真的开始执行**（排队不算失败，最长等到 timeout=5s，绝不撤销）；
2. 页面受理后等回执；socket 明确断开时只再等 2 秒；
3. 压根没执行到 → `failed / retryable / send_not_executed`（中心可重投），绝不报「已提交」。

实测（1.0.12，真实会话）：

```
04:27:03 command received
04:27:05 send accepted（页面 eval 1s 内完成）
04:27:07 accepted，耗时 3.08s，real_send=True
04:27:07 self reply published locally delivery=confirmed
本地会话 waiting_for_reply = False   ← 浮窗倒计时清零
```

对比改前同样场景：`6.95s / indeterminate / real_send=False`。

## 1.0.5 相对 1.0.4

现场（TENDA 单店，1.0.4）日志显示：实时 socket 常年 `state=none`，没点开的会话全靠 CDP 回拉，
全量回拉 ~129s 一轮，买家最慢等两分钟；补拉回放还会把同一条消息反复入队。1.0.4 的「只回拉在线店铺」
对单店机器没有作用，这一版处理的是下面三件事：

1. **回拉快车道**：对「最近 10 分钟内说过话」的买家（最多 10 个）每 10s 回拉一次，全量回拉仍按
   `history_pull_seconds`。可配：`history_pull_hot_seconds`（0=关）、`history_pull_hot_window_seconds`、`history_pull_hot_max`。
2. **补拉回放去重**：补拉回放的 `event_id` 在近 1 小时内已入队过就直接丢，不再重复入队/推本地工作台
   （现场 546 条消息产生 923 次入队，单条最多 14 次）。
3. **旧 websocket 地址**：`websocket_url` 指向旧中心 `47.107.138.228` 而 `server_url` 已是新中心时，自动改为跟随 `server_url`；
   设置窗口不再预填旧地址。

**没有改的（需要另行处理）**：拼多多工作台被杀/卡死后的自动拉起（涉及 `start_pdd_portable.cmd` 的 MD5 锁，需和探域侧一起定方案）；
`history_report_max_age_seconds` 仍为 1800（调小会丢失断档后的补回消息）。

## 1.0.6 相对 1.0.5

依据现场交接单 v2（TENDA 单店）。真正的病因是**插件日志目录错位**：探域修插件时把运行中工作台
`PDDData.ini` 的 `LogDir` 改到 `D:\kefuAgent\tanyu3.2.2\logs`，桥接仍盯着写死的
`C:\Users\Public\PddPortableRuntime\3.5.0.40\plugins\logs`，inside/logrus/plugin 三路实时源同时归零。

1. **日志目录自动跟随（D1）**：启动时和每 30s 读运行中 `PddWorkbench.exe` 同目录的 `PDDData.ini` → `LogDir`；
   读不到时，若当前目录 120s 没写入、而候选目录（便携包 / `D:\kefuAgent\*\logs` / 探域 runtime）10 分钟内写过，就换过去。
   切换后新目录从文件尾开始跟（不整份回放），漏掉的一小段由 CDP 补拉兜底。
   日志：`log_dir resolved=... source=pddini|config|recent_write ... files=[...]`、`日志目录切换 A -> B`；
   启动行改为「已开始监听本机 PDD 插件日志（拼多多）：<路径>」。
2. **本地会话不再被清空（D2）**：网关每次启动先给 `data\seat_local_state.json` 留快照（`.bak-<时间>`，保留 5 份）；
   读取改用 utf-8-sig（PowerShell 5.1 写出的 BOM 文件以前会读失败，随后被空状态覆盖）；
   读不了时原文件改名保留为 `.unreadable-<时间>` 并打错误，而不是静默盖掉。安装器升级前把 `data\` 整目录复制为 `data.bak-1.0.6-<时间>`。
3. **陈旧历史不送中心（D3）**：`history_report_max_age_seconds` 对所有历史来源生效（以前只管 CDP 补拉），
   日志回补带回的旧消息只在本地工作台展示、不进中心、不触发 AI 回复。默认值仍是 1800。
4. **补拉回放不再重复进本地工作台（D4）**：1.0.5 的回放去重只挡住了上传中心；local-first 包装层先推本地、后过滤，
   所以 127 工作台每轮补拉仍会多出一批。现在去重提前到推本地之前。

**没做的**：D5 工作台看护自动拉起、D6 自动打开会话、D7 网关自身 `log_dir`/remote sync 404、D8 的 `/api/status` 延迟指标与 UI 状态灯。

## 1.0.7 相对 1.0.6

1. **拼多多工作台看护（D5）**：每 5s 检查 `PddWorkbench.exe`。
   - 只有「本次桥接运行期间见过它在跑」才会拉起（开机时客户自己没开，不替他开）；
   - 消失后等 8s（给探域自己拉起的机会），仍未回来就运行 `start_pdd_portable.cmd`（复用其 MD5 校验与只读锁，绝不直接启动 exe）；
     启动器自动在桌面 `*\start_pdd_portable.cmd` 里找，找不到就只告警不拉起；
   - 主窗口连续无响应 90s → 结束进程，按「消失」处理；
   - 两次拉起至少隔 60s，每小时最多 6 次；
   - 工作台回来后立即重连 CDP、重新解析日志目录，15s 后补拉一轮（不再等下一个 120s）。
   - 配置：`workbench_watchdog_enabled`（false 关闭）、`workbench_launcher`、`workbench_relaunch_grace_seconds`、
     `workbench_hang_kill_seconds`（0=只告警不结束）、`workbench_min_relaunch_interval_seconds`。
2. **可调配置项不再被丢弃**：以前 `load_config`/`save_config` 只保留白名单里的键，1.0.5 的
   `history_pull_hot_*` 写进配置文件也不生效。现在这些键和看护的键都会被读取并在保存时保留。

**风险**：探域 `restart.bat` 杀掉工作台后，如果探域在 8s 之后也自己拉起一份，可能出现两个工作台；
现场若遇到，把 `workbench_relaunch_grace_seconds` 调大或关掉看护。

## 1.0.8 相对 1.0.7

**补拉到的新鲜消息按实时上报**（移植 0.7.14 / 0.8 线的「新鲜补拉提升为实时」，1.0 线基于 0.7.6，一直缺这一块）。

现象：没点开的会话靠 CDP 补拉拿到买家消息，补拉带回的消息统一被标成历史，上报队列对历史有 90 秒门槛
（平台时间超过 90 秒才放行，防止旧中心把历史当实时回复），所以快车道 10 秒就拿到、还是 90 秒才上报。

现在：
- 补拉到的消息如果平台时间在 30 秒内、来源是主动补拉（不是本地队列恢复/重放）、且这个会话还没有卖家更晚的回复，
  就按实时立即上报（`history_reason=fresh_pull_promoted`，日志 `fresh pull promoted msg_id=...`）；
- 同一条消息先以历史身份进了待发队列、随后实时帧才到 → 把队列里的历史副本替换成实时（日志 `pending history event upgraded to live`）；
- 超过 30 秒的补拉、真正的旧历史、重放、已有卖家回复的，仍按原规则走 90 秒窗口。
- 配置：`promote_fresh_pull`（false 关闭）、`fresh_pull_max_age_seconds`（默认 30）。

## 1.0.9 相对 1.0.8

现场 1.0.8 日志：补拉 7.9s / 27s 就拿到的买家消息仍被标成历史、压到 90 秒才上报，`fresh pull promoted` 一条都没有。

1. **修「已有卖家回复」误判**：判断会话里有没有更晚的卖家回复时，卖家帧的 `ts` 是毫秒、买家消息的 `ts` 是秒，
   比较前没有换算 —— 任何一次更早的卖家回复都被当成「比买家消息晚」，于是只要这个会话卖家回过一次，
   之后的补拉消息就永远不提升。现在先换算成秒再比。（0.8 线同一处有同样的问题。）
2. **提升窗口 30s → 60s**：现场一条 31.4s 才补拉到，差 1.4s 被压了 90 秒；中心的实时窗口是 90s，
   且「已有卖家回复」的保护仍在。可用 `fresh_pull_max_age_seconds` 调回。

## 1.0.10 相对 1.0.9

1. **修日志目录被切到空目录（1.0.6 引入）**：现场 1.0.9 的心跳显示桥接盯着 `PddPortableRuntime\3.5.0.40\logs`，
   一个插件日志文件都没打开（attached 为空），inside/plugin 实时通道整个停掉。原因：90 科技便携包的 `PDDData.ini`
   里 `LogDir` 指向 `...\3.5.0.40\logs`，插件实际写在 `...\3.5.0.40\plugins\logs`，而 1.0.6 无条件相信 LogDir。
   现在只会换到「里面真有插件日志」的目录，并把 LogDir 下 / 同级的 `plugins\logs` 一起列为候选，取最近写入的那个。
   本机复现：LogDir 0 个文件、plugins\logs 50 个文件 → 新逻辑选 plugins\logs。
2. **自带诊断，客户不用操作**：心跳里带 `diagnostics`（中心 `/api/bridge/v1/admin/agents` 可读），本地每 30s 写
   `logs\diagnostics.json`。内容：正在盯的目录及原因、工作台 ini 的 LogDir、各候选目录文件数与最后写入、已打开的日志文件、
   近 30 分钟每个通道的实时/历史/提升条数与采集延迟 p50/p90、CDP 会话 socket 状态、补拉轮次、看护状态、待发队列。
   **不含任何消息内容**。

## 1.0.11 相对 1.0.10

**AI 回复发没发出去，按真实结果记录和标记。**

现场：消息 1795782848672 的 AI 回复，大脑 6 秒生成、桥接立刻领走，工作台页面返回「已受理」，桥接等 2 秒没回执就按
「已发送」上报，本地工作台也标「已确认发出」—— 实际没到买家（插件日志里这条从未出现在卖家侧）。
原因：工作台页面聊天连接是 `socket=none`，页面受理不等于真的发出；而拼多多客户端过后回的拒绝，桥接在 2 秒后就不再接收。

现在：
1. 「页面已受理、没回执」先在聚合接待里标 **已提交待确认**，倒计时不停；
2. 之后插件日志 / 页面里在卖家侧看到同一句话 → 改标 **已确认发出**，倒计时停（日志 `AI 回复已在卖家侧出现 delay=...`）；
3. 拼多多客户端过后回了拒绝 → 改标 **发送失败**，附原因（日志 `AI 回复被拼多多客户端拒绝`）；
4. 60 秒内都没看到 → 改标 **已提交，未确认买家是否收到**，倒计时继续，客服能看到、能补发（日志 `AI 回复 60s 内没在卖家侧出现`）。
   时限可配 `send_verify_seconds`。
5. 不自动重发；上报中心的指令结果不变（不改中心契约）。
6. 诊断里新增 `sends_30m`：每条回复的最终状态、回显延迟 p50/p90、拒绝原因、最近 15 条明细（买家 ID 只留后 6 位，不含内容）。

## 1.0.12 相对 1.0.11

**把 0.7.6 的原生跳转（和原生发送）带回 90 科技便携包。**

0.7.6 的秒跳助手 / 网关跳转 / 原生发送，都靠工作台里 `PddInsidePlugin` 开的本地端口（`/tanyu/client/pdd/dll/httpTestApi`）。
便携包里也是这个插件（与探域那份文件哈希一致），但它只在连上探域的注入器 `Injector_pdd.exe` 后才开端口；没有探域就永远不开，
所以跳转一直报 `PddWorkbench DLL port unavailable`。1.0.x 的跳转代码与 0.7.6 完全相同，坏的是环境，不是代码。

现在桥接自己扮演注入器（`bridge/inside_ipc.py`）：
- 每 5 秒找运行中的 `PddWorkbench.exe`，从它的 `PDDData.ini` 找到插件目录，写握手文件
  `<插件目录>\temp\InsideStartup_<工作台进程号>.ini`（节名 = 工作台进程号，本机二分实测确定）；
- 插件连上桥接的本地 WebSocket 后，按探域顺序发 `init`（插件加载 PddMsgPlugin）和 `start_deal_msg`，插件随即开出 0.7.6 的端口；
- 秒跳助手和网关跳转**一行没改**，按 0.7.6 原路生效。本机实测：秒跳 201ms，工作台左侧切到目标买家。
- 端口开出后，发送通道按 0.7.6 规则优先走插件原生发送（有明确成功/失败回执）。
- 探域在运行、或工作台加载的是探域目录里的插件时，不写握手、不抢连接。
- 开关：`inside_ipc_enabled`（false 整个关闭）、`inside_native_send`（false 时发送仍走页面，只用于跳转）。
- 诊断新增 `inside_ipc`：服务端口、已连上的工作台、握手情况、插件推来的各类消息条数。
- 插件连上后会实时推所有会话的买家消息（`chat_message`），本版只计数，后续可作为实时收消息通道。
