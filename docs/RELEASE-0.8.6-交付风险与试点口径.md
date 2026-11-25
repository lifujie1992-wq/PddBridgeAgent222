# 90科技拼多多桥接助手 0.8.6 交付风险与试点口径

> 面向：发给客户/试点商家之前必须做的检查 + 出了问题怎么回滚。
> 交付物：`dist/90科技拼多多桥接助手-Setup-0.8.6.0.exe`
> 本文件由 2026-11-26 的真机验收（本机 0.8.1 → 0.8.6 连续升级 + 真实买家会话）整理。

## 1. 验收结论（先说重点）

真机验收里发现，客户反馈的「AI 回复有的能发、有的没实际发出、倒计时还在走」不是单一 bug，
而是四件事叠在一起：

| # | 根因 | 现象 | 0.8.6 |
|---|------|------|-------|
| 1 | 安装/升级时 RestartManager 会**关掉拼多多工作台**（它加载着 `pdd_send_v6.dll`），而它没注册 `RegisterApplicationRestart`，`RestartApplications=yes` 拉不起来 | 装完客户没重开工作台 → 发送全部 blocked/失败，一条也发不出去 | 安装器装前记下工作台路径，装完**自动重新打开**（已实测：02:36:35 自动拉起） |
| 2 | `send_and_wait` 只等 5s，超时就把**还在排队的发送撤销**，却返回 `ok=True/indeterminate`「已提交工作台」 | 实际没发出去，中心不重投 → 回复永久丢失；日志里连 `send accepted` 都没有 | 区分「没轮到执行」→ `failed/retryable`；「执行了没回执」→ `accepted` 或 `indeterminate`，且不再撤销已开始的发送 |
| 3 | 工作台刚重启时页面 `account="?"`，`_session_for_send` 选不到会话 → `blocked` 且**不可重试** | 重启窗口期的回复全部静默丢失 | `blocked` 现在带 `retryable=True` + `error=session_unresolved`，并打出会话清单日志 |
| 4 | 页面已受理（`__pddBridge_sendText` 返回 ok）但没等到回执时，一律 `indeterminate` → 本地回写 `delivery=indeterminate` | 消息其实发出去了，浮窗倒计时却一直走 | 页面受理即 `status=accepted / real_send=True`，本地回写 `confirmed` → **倒计时停**（实测 4/4 停表） |

另外两处：

- 0.8.1 的倒计时修复（`_publish_self_reply`）是**死代码**：它只认 `_local_ingest_queue`，而正式运行
  路径里该队列恒为 `None`（本地投递走 `LocalDeliveryQueue`）。0.8.2 起改走 `_local_delivery`，
  日志 `self reply published locally` 现在会真的出现（0.8.1 整场 0 条）。
- 老配置里的 `pdd_dll_path`（例如开发机的 `D:\temp\...\pdd_send_v5.dll`）会一直被沿用，
  每次发送先失败一次（`OK 0`）再回退页面，白丢约 2 秒。现在「同文件名族且比安装包旧」会自动
  改用安装包里的 `pdd_send_v6.dll`，并打警告说明换成了哪个。

**真机实测（0.8.6，工作台由安装器自动拉起后）**

```
send done in 2.08s status=confirmed real_send=True via=cdp+ws
self reply published locally buyer=4764375385604 delivery=confirmed
```

连续 4 条全部 `confirmed`。

## 2. 仍然没解决的（必须让客户/自己知道）

### 2.1 原生直发（DLL）还起不来 —— 页面发送是唯一的可用通道
- 现象：`pdd_send_v6.dll` 注入成功，但 `pipe STATE = READY 0 INSTANCE 0000000000000000 CAPTURE 0 HOOKS 0 IMG 1`，
  即捕获不到工作台的 `CMChatImpl` 实例；`TEXT` 命令返回 `OK 0`（探域同款问题）。
- 影响：所有发送都走页面 CDP（`__pddBridge_sendText`），速度慢、回执依赖 WS 帧；
  一旦页面不在会话上就可能整条失败。
- 归属：DNS 侧在 `D:\temp\pdd-send-hook`（DLL 工程），需要按 3.5.0.40_x64 的实例捕获重做；
  桥接侧已经把「注入时机（启动即注入）」「v6 优先」「失败可重试」都准备好了。

### 2.2 注入需要 UAC，且必须在工作台启动时完成
- 桥接助手不是管理员运行时，注入走 `ShellExecuteW("runas")` 弹一次 UAC；客户点「否」就只能走页面发送。
- 若 UAC 确认晚于工作台初始化，仍会 `READY 0`（需重启工作台）。
- 交付说明里必须写：**装完允许 UAC、让工作台由安装器自动打开**。

### 2.3 明文传输
- 中心 `http://47.112.219.14:18765` + `ws://`；`agent_token` 明文存
  `%LOCALAPPDATA%\Programs\PddBridgeAgent\bridge_config.json`。
- 全量交付前建议上 HTTPS/WSS（或至少书面告知风险）。

### 2.4 无代码签名
- exe/安装包均未签名：SmartScreen「未知发布者」，注入行为容易被 360/火绒拦截。
- 全量交付前补证书；试点阶段附放行说明。

### 2.5 原生接收仍是降级模式
- 日志 `原生接收已启动（导出 detour（只收心跳/出口帧））... hook=0`。
- 买家消息靠 CDP 回拉（`history_pull_seconds=30`，最多 40 会话）兜底，20 店实时性未根本改善。

### 2.6 即时上传依赖中心按 `msg_id` 判重
- 命中店铺先传原文，再补 `pdd-context-*` 增强事件。中心若未去重 → 同一条消息两次入队 AI → 重复回复。
- 不确定就把 `immediate_ingress_enabled` 设 `false` 再发试点。

### 2.7 测试基线仍是红的（历史欠账）
- 全量 `26 failed / 281 passed`；对照 0.7.23 基线 `32 failed`，同源（老 mock 引用已删除的符号）。
- 0.8 相关用例全绿：`test_latency_p0 / test_reply_countdown / test_pdd_send_transport /
  test_command_claim / test_direct_inject_resolve`。

## 3. 构建溯源

| 项 | 值 |
|----|----|
| 源码 commit | `v0.8.6`（基线 `v0.8.1` = 客户现场版，`v0.8.2`–`v0.8.5` = 中间修复） |
| 版本号 | `bridge/__init__.py`、`run_frontend_service.py` 均为 `0.8.6.0` |
| 构建 | `build_release_0.7.7.ps1 -Python <py3.12>`（PyInstaller 6.22.3） |
| 原生件 | `D:\temp\pdd-send-hook\out\pdd_send_v6.dll` + `injector.exe`（未改动） |
| 安装包 | `installer/90tech-pdd-v0.8.6.0.iss`（Inno Setup 6 / ISCC） |

### 交付物校验

| 文件 | SHA256 |
|------|--------|
| `dist/90科技拼多多桥接助手-Setup-0.8.6.0.exe` | `707ea7c37066f0f3b8584a88cc852297e82f5f313b1849c800f12f5fb6523ba2` |

## 4. 试点口径

**可以发**：1–2 家、店铺数 ≤3、有对接人、能配合 UAC 与重启工作台、能回传日志。
**暂不发**：20 店以上、无人值守、禁止弹 UAC 的客户。

试点前确认：

- [ ] 中心侧已按 `msg_id` 判重（否则 `immediate_ingress_enabled=false`）
- [ ] 客户接受「安装会自动关闭并重新打开拼多多工作台」
- [ ] 客户接受「首次注入需要 UAC 确认」
- [ ] 交付包 = `Setup-0.8.6.0.exe` + 本文件 + 安装说明
- [ ] 记录试点店铺 ID，便于灰度

### 灰度与回滚

单店关闭：

```json
{
  "immediate_ingress_disabled_shop_ids": ["cs_447787481:184712850"],
  "command_sender_disabled_shop_ids": []
}
```

整机回滚（改完重启桥接助手，无需重装）：

```json
{
  "immediate_ingress_enabled": false,
  "command_sender_enabled": false
}
```

> 布尔按字面解析：`false / "false" / 0 / "off"` 都算关；缺失键 = 开（owner 决定的默认全开）。

## 5. 交付验收清单（30 分钟）

1. 新机器静默安装 → 桌面/开始菜单快捷方式存在，桥接自动启动，无崩溃。
2. 安装时工作台若在运行 → **装完自动重新打开**（`Get-Process PddWorkbench` 有 StartTime）。
3. 允许 UAC 注入 → 日志 `直发通道就绪`（或至少 `send accepted` 走页面通道）。
4. 中心下发一条发送 → 日志 `send done ... status=confirmed real_send=True`（页面通道为 `cdp+ws`）；
   同一时刻必须出现 `self reply published locally ... delivery=confirmed`。
5. 浮窗倒计时：AI 回复后 1–2 秒内停止。
6. 买家发一条消息 → 本地工作台 2 秒内出现，中心 `center_accepted`。
7. 覆盖升级 0.7.x → 0.8.6：配置/令牌/队列保留，旧进程被杀，网关 `gateway_version=0.8.6.0`。
8. 停掉工作台再发一条 → 日志应出现 `send blocked ... retryable=True` 或 `failed`，
   而不是「已提交工作台」；恢复工作台后回复能补发/重试成功。
9. 关闭 `command_sender_enabled` 重启 → 指令仍可发送（回落串行路径）。

## 6. 不在本版范围

- 原生直发 DLL 的实例捕获（需 DLL 侧修改）
- 原生接收全量 hook
- HTTPS/WSS 与令牌加密存储
- 代码签名
- 20 店以上容量验证
- 老测试用例修复（26 failed）
