# 90科技多多助手 1.0.4（基于 0.7.6 稳定版）

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

## 1.0.4 修复（相对 1.0.2）—— 1.0.2 会丢消息

1.0.2 为了「socket 断开别干等」把等待砍到 2 秒，结果**排队中还没轮到执行的发送被撤销**，
消息直接丢掉（现场 04:10:20 那条 `1795637406820` 就是这么没的：日志里没有 `send accepted`，
3.05s 后报 indeterminate）。

现在拆成三步：

1. 先等它**真的开始执行**（排队不算失败，最长等到 timeout=5s，绝不撤销）；
2. 页面受理后等回执；socket 明确断开时只再等 2 秒；
3. 压根没执行到 → `failed / retryable / send_not_executed`（中心可重投），绝不报「已提交」。

实测（1.0.4，真实会话）：

```
04:27:03 command received
04:27:05 send accepted（页面 eval 1s 内完成）
04:27:07 accepted，耗时 3.08s，real_send=True
04:27:07 self reply published locally delivery=confirmed
本地会话 waiting_for_reply = False   ← 浮窗倒计时清零
```

对比改前同样场景：`6.95s / indeterminate / real_send=False`。

## 1.0.4 相对 1.0.3

**回扫只针对在线店铺。** 账号里缓存了很多店、只登录一家时，未登录店铺的买家占满每轮回扫的
`history_pull_max_buyers` 名额，在线店里没点开的会话要等好几轮才被拉到，表现为「消息时快时慢」。
现在回扫目标过滤为有在线会话的店铺；日志 `history pull: N conversation(s) backfilled (targets=总数 online=在线数)`
可看到目标池里有多少是无效的。其余行为与 1.0.3 一致。
