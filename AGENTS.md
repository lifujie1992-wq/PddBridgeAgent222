# 90科技多多助手（拼多多桥接）— 给 AI / 维护者的说明

> 本仓库：`release/1.0.0` 分支 = 1.0.x 客户线（基于 0.7.6 稳定版）。
> 另有 `release/0.8.2` = 0.8.x 线（与 1.0.x 不互通，改动不能直接搬）。
> 远端：`github.com/lifujie1992-wq/PddBridgeAgent222`。
> 最后更新：2026-11-28（1.0.13）。

## 1. 一句话

桥接装在客服电脑上，从拼多多商家工作台收买家消息 → 上报中心大脑（`47.112.219.14:18765`）
→ 领回 AI 回复 → 发回工作台；同时推一份给本机聚合接待（`127.0.0.1:18767`）。
**客户机器上不装探域**（2026-11-28 确认）。

## 2. 收 / 发 / 跳转 的实际机制（无探域机器）

| | 0.7.6 | 1.0.12 | 1.0.13 |
|---|---|---|---|
| 收：主通道 | 读插件日志文件 | 同左 | 同左 |
| 收：读哪个目录 | 默认探域目录，没有探域就找不到 → 实时收消息基本断 | 自动跟随插件实际写日志的目录（读 `PDDData.ini`，只认真有日志文件的目录） | 同 1.0.12 |
| 收：插件本身 | 没探域不启动 | 桥接扮演探域注入器把插件连上，插件实时写所有会话买家消息（实测 ≤1 秒） | 同 1.0.12 |
| 收：网页回拉补漏 | 120 秒/轮，最近 40 会话 | 另对最近 10 分钟活跃买家每 10 秒拉；只拉已登录店铺 | 同 1.0.12 |
| 收：补拉到的新消息 | 当历史压到 90 秒才报，中心 30 秒后不回 | 60 秒内的直接按实时上报 | 同 1.0.12 |
| 发：通道 | 网页发送（`__pddBridge_sendText` → `socketUtil.sendMsg`） | **插件原生发送**（端口被桥接开出来后按 0.7.6 规则自动改走） | **网页发送** |
| 发：怎么算发出去 | 等 5 秒回执，等不到报「结果未知」后不管 | 插件日志写「发送成功」就算 | 卖家侧真出现同一句才算；客户端拒绝=发送失败；60 秒未出现=未确认 |
| 发：实际效果 | 受理了没发出时没人知道 | **两家店都出现「插件报成功、买家没收到」，被记成已确认发出** | 不会再假确认；但网页发送在 `socket=none` 时约 70 秒才真发出，偶有不发 |
| 跳转 | 秒跳助手 → 插件端口 → 原生打开会话；没探域就失败 | 能跳（实测 0.2 秒） | 同 1.0.12 |

0.7.6 选发送通道的规则（`bridge/platforms/pdd.py::channel_status`）：工作台里找得到插件端口 → 插件原生发送；
否则 → 网页发送。1.0.13 起：插件端口是桥接自己（`inside_ipc`）接管出来的时默认走网页发送，
配置 `inside_native_send: true` 才走原生发送。

## 3. 关键机制备忘

### 3.1 插件握手（替代探域注入器，`bridge/inside_ipc.py`）
- 便携包 `PddInsidePlugin_x64.dll` 与探域那份是同一个文件（哈希一致），只有连上注入器后才开 HTTP 端口
  （`/tanyu/client/pdd/dll/httpTestApi`，跳转/原生发送都用它）。
- 插件每 2 秒读 `<插件目录>/temp/InsideStartup_<工作台进程号>.ini`，**节名 = 工作台进程号**（本机二分实测），
  键：`serverPort / platformPid / platformType=1 / identify=workbench / platformVersion / targetProcessId / isReady` 等，读完即删。
- 插件连 `ws://127.0.0.1:<serverPort>/platformType=1&platformPid=..`；连上后必须按探域顺序发
  `{"cmd":"init","data":{"logType":4,"platformVersion":"3.5.0.40"}}` 和 `{"cmd":"start_deal_msg"}`，
  插件才会加载 PddMsgPlugin、开端口，并以 `chat_message` 实时推所有会话的买家消息。
- `targetProcessId` 指向的进程退出后插件停止重连 → 填桥接自己的进程号。
- 探域在跑或插件在探域目录时桥接不介入。开关：`inside_ipc_enabled`、`inside_native_send`。

### 3.2 日志目录
- 便携包 `PDDData.ini` 的 `LogDir` 指向 `...\3.5.0.40\logs`（空目录），插件实际写 `...\3.5.0.40\plugins\logs`。
  1.0.6~1.0.9 无条件相信 LogDir 导致实时通道全断，1.0.10 修。

### 3.3 中心的时效规则（决定「超时不回」）
- 中心配置 `shadow_live_message_max_age_seconds = 30`：消息到中心时距买家发出超过 30 秒 → `stale_bridge_event`，不回；
  回复指令有效期 = 买家消息时间 + 30 秒，过期丢弃。
- 实时到达的消息，中心约 8 秒（P90 9.4 秒）生成回复，桥接 1~2 秒领走 → 桥接须在买家发出后约 18 秒内送到中心。
- 中心有店铺级「最新一条迟到消息仍回复」开关 `stale_latest_reply_guard_mode`（目前各店 off）。

### 3.4 发送结果核对（1.0.11 起）
- 「页面已受理没回执」先标「已提交待确认」→ 卖家侧出现同一句 → 「已确认发出」；
  客户端迟到的拒绝 → 「发送失败」；`send_verify_seconds`（默认 60）内没出现 → 「未确认」。不自动重发。

## 4. 现状与待办（2026-11-28）

- 现场机器 `DESKTOP-Q6JFCD0`（`agent-8bbf2f677ae7d3a9`，TENDA `cs_900320299` + EAGET `cs_335417670`）：
  装的 1.0.12，已手动配 `inside_native_send: false`（等同 1.0.13 的发送行为）。1.0.13 尚未安装。
- **未解决：网页发送慢/偶尔不发**。根因是工作台页面聊天连接 `socket=none`（未打开会话时拼多多不建连接）。
  候选方案（未实现，需先验证）：发送前用插件原生跳转打开该买家会话，再走网页发送。
  代价：工作台左侧会话会随发送切换，可能干扰正在操作的人工客服。
- **多工作台进程**：0.7.6 找插件端口的规则是「工作台只监听一个端口就用它」，多个工作台进程时跳转/原生发送会失效；
  25 店若分多个窗口需改成按店铺路由。
- 1.0.12 原生发送期间（约 2026-11-27 22:21 ~ 11-28 00:31）的回复可能有没送到的，需人工核对。
- 0.8.x 线：毫秒/秒比较错误（`_note_seller_reply`）等 1.0.x 上的修复都没同步。

## 5. 排查方法

- **中心服务器**：本机 `ssh pddcenter`（密钥 `~/.ssh/id_ed25519_pddcenter`，root）。只读为原则；
  生产代码在 `/usr/pdd-brain-company2`（有大量未提交的线上改动，改之前先读那里的 `AGENTS.md` 和 `DEPLOY_NOTES.md`）。
- **桥接诊断（不用客户操作）**：中心 `/api/bridge/v1/admin/agents` 的每个工位 `status.diagnostics`：
  `log_dir`（目录选择原因/候选目录）、`attached_files`、`channels_30m`（各通道实时/历史/提升条数与延迟）、
  `sends_30m`（每条回复的核对结果）、`inside_ipc`（插件连接）、`history_pull`、`workbench_watchdog`。
  需在服务器上带 `X-Bridge-Token`（值在 `runtime/config.json` 的 `api_token`，不要外传）。客户机本地另有 `logs\diagnostics.json`。
- **中心数据库**（容器 `pdd-brain-postgres`）：`pdd_brain_bridge_events`（收到的消息）、`pdd_brain_brain_jobs`（大脑任务与原因）、
  `pdd_brain_bridge_commands`（回复指令与发送结果）、`pdd_brain_message_whiteboxes`（不回复原因 `no_reply_reason_code`）。
  中心日志时间是 UTC。`pdd_brain_messages` 是批量落库，`inserted_at` 不等于到达时间。

## 6. 构建发布

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_1.0.0.ps1 -Python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\90tech-duoduo-v1.0.x.iss
```
- 版本号三处一起改：`bridge/__init__.py`、`run_frontend_service.py`（`LOCAL_GATEWAY_VERSION`）、新的 `installer/90tech-duoduo-v<版本>.iss`，并写 `RELEASE-<版本>.md`。
- 新模块要加进 `build_1.0.0.ps1` 的 `--hidden-import`。
- 测试：`$env:PYTHONPATH=$PWD; py -3 -m pytest -q tests test_local_first_client.py test_gui_config.py --continue-on-collection-errors`。
  基线有 34 个历史失败（老 mock 引用已删符号等），比较时看「有没有新增失败」。
- 推送远端、在客户机器上安装、改中心生产，都要先问用户。

## 7. 约束

- 不要升级拼多多工作台，不要动 `PddWorkbench.exe` / `PDDUpdate.exe`（便携包有 MD5 锁）；启动工作台走 `start_pdd_portable.cmd`。
- 中心是生产环境：行为改动必须带店铺级灰度开关（默认 off，白名单启用，先 observe 再 enforce），部署/重启前先问用户；
  重启 `app` 会清空中心内存里的在线工位登记，十几秒内的回复会以「preferred PDD bridge agent is unavailable」发送失败。
- 不要把密码、令牌写进代码、文档或对话。
