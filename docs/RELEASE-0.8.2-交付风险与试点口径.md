# 90科技拼多多桥接助手 0.8.2 交付风险与试点口径

> 面向：发给客户/试点商家之前必须做的检查 + 出了问题怎么回滚。
> 配套版本：`v0.8.2`（对应安装包 `dist/90科技拼多多桥接助手-Setup-0.8.2.0.exe`）。

## 1. 这一版到底改了什么

0.8.1 引入两个 P0（默认全开）：

| 特性 | 行为 | 一键回滚键 |
|------|------|-----------|
| 即时上传 `immediate_ingress` | 买家消息不再等订单上下文查询，先传中心；上下文随后按 `msg_id` 补一条增强事件 | `immediate_ingress_enabled: false` |
| 出站并发发送池 `command_sender` | 指令投入 6 个 worker 发送，取指令循环不再被上一条的 10s 确认超时占住；同一买家仍串行 | `command_sender_enabled: false` |

0.8.2 修的是 0.8.1 现场暴露的缺陷（详见 commit `2884af6`）：

1. **浮窗倒计时不停**：`_publish_self_reply` 只认 `_local_ingest_queue`，而正式运行路径里该队列恒为 `None`（本地投递走 `LocalDeliveryQueue`）→ 修复从来没执行过。现在优先 `_local_delivery.enqueue()`。
2. **回执配不上**：待确认表 key 改成带 `account` 后，命令没带 account 时回执永远匹配不上，已发出的消息被报成 `indeterminate`。现在内容唯一兜底，account 任意一侧为空即可用。
3. **指令被永久占位**：`command_journal.start()` 失败时认领不释放，这条回复直到重启都不会再发。现在失败即释放。
4. **发送池饱和阻塞轮询**：池满时改走有上限的兜底线程，不再在取指令线程里内联等发送。
5. **链式等待无超时**：同买家上一条卡死会拖死后续全部消息。现在有上限（`send_confirm_timeout*3`，最低 30s）。

## 2. 构建溯源（出问题必须能定位）

| 项 | 值 |
|----|----|
| 源码 commit | `2884af6`（tag `v0.8.2`，基线 `82bda2a` / `v0.8.1`） |
| 版本号 | `bridge/__init__.py: __version__ = 0.8.2.0`、`run_frontend_service.py: LOCAL_GATEWAY_VERSION = 0.8.2.0` |
| 构建脚本 | `build_release_0.7.7.ps1 -Python <python3.12>`（PyInstaller 6.22.3） |
| 原生件 | `D:\temp\pdd-send-hook\out\pdd_send_v6.dll`（2026-11-25）+ `injector.exe` |
| 安装包 | `installer/90tech-pdd-v0.8.2.0.iss` → Inno Setup 6 / ISCC |
| 安装包内容 | 仅带 `pdd_send_v6.dll`，v3/v5 已不再打包 |
| 签名 | **无数字签名**（见 §3.1） |

### 交付物校验

| 文件 | SHA256 |
|------|--------|
| `dist/90科技拼多多桥接助手-Setup-0.8.2.0.exe` | `d7b4f73ea883964ef536b5392cd2ec70825df0170106929d56e4f6aa8decc14f` |

包内原生件：`pdd_send_v6.dll`（844470 B）、`injector.exe`（99643 B，未改动）。

> 复现命令：
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File build_release_0.7.7.ps1 `
>   -Python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
> "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\90tech-pdd-v0.8.2.0.iss
> ```

## 3. 已知风险（发给客户前必须知情）

### 3.1 安装包与 exe 无数字签名
- 现象：SmartScreen「未知发布者」；部分安全软件（360/火绒/Defender）会拦。
- 原因：程序会 `LoadLibrary` 注入 `PddWorkbench`，无签名时启发式判定偏高。
- 处置：随包附《安装说明》（放行方式）；有证书后必须补签（**发全量客户前完成**）。

### 3.2 注入需要 UAC，且要求工作台在注入后重启
- 现象：日志 `直发注入（监视器）... 已请求提权注入（UAC 确认后生效）`；若工作台已启动过，则 `直发通道未就绪（READY 0 ...）`。
- 影响：拒绝 UAC 或未重启工作台 → 只能走 CDP 页面发送（更慢、回执更弱）。
- 处置：安装/升级后**必须重启拼多多工作台**；在安装完成页和 GUI 首启提示里写清楚。

### 3.3 明文传输
- 中心为 `http://47.112.219.14:18765` + `ws://`，`agent_token` 明文存于
  `%LOCALAPPDATA%\Programs\PddBridgeAgent\bridge_config.json`。
- 影响：聊天内容与工位令牌明文过公网；同机其他账户可读令牌。
- 处置：短期在交付说明中写明；中期上 HTTPS/WSS（**发全量客户前完成**）。

### 3.4 即时上传依赖中心按 `msg_id` 判重
- 命中店铺会先传原始消息，再补一条 `pdd-context-*` 增强事件。
- 若中心版本未按 `msg_id` 去重/合并，会出现**同一条消息两次入队 AI → 重复回复**。
- 处置：试点前确认中心已上线按 `msg_id` 判重版本；不确定就用单店 `*_shop_ids` 灰度。

### 3.5 原生接收仍是降级模式
- 日志 `原生接收已启动（导出 detour（只收心跳/出口帧））... hook=0`（部分环境 `全槽位嗅探 hook=-4`）。
- 买家消息仍主要靠 CDP 历史回拉（`history_pull_seconds=30`，最多 40 会话）→ 20 店场景实时性与负载未根本改善。

### 3.6 测试基线是红的（历史欠账）
- 全量：`26 failed / 272 passed`，另有 3 个 collection error。
- 对照 0.7.23 基线：`32 failed` → 同源历史欠账（老 mock 引用已删除的
  `DEFAULT_EVAL_TIMEOUT`、`_INGEST_DEDUP_MIN_SECONDS`、`relocate_paths_from_other_install`）。
- 0.8 相关用例全绿（`test_latency_p0 / test_reply_countdown / test_pdd_send_transport / test_command_claim`，48 passed）。
- 结论：**测试不能作为「无回归」的证据**，只能作为「改动点有覆盖」的证据。

### 3.7 其它
- 中心地址、工位令牌属于每个客户自己的配置，不要随包分发 `bridge_config.json`。
- 升级会杀掉安装目录下的进程；若工作台占用 DLL，会走 `restartreplace`（提示重启）。
- 卸载不删配置/日志/队列（设计如此），换机时请手工备份 `bridge_config.json`。

## 4. 试点口径（建议）

**可以发**：1–2 家、店铺数 ≤3、有对接人、能配合重启工作台与反馈日志。
**暂不发**：20 店以上、无人值守、不能接受 UAC/重启的客户。

试点前逐项确认：

- [ ] 中心侧已支持按 `msg_id` 判重（否则把 `immediate_ingress_enabled` 设 `false`）
- [ ] 客户机器允许程序请求 UAC，且接受「装/升级后重启拼多多工作台」
- [ ] 交付包 = `dist/90科技拼多多桥接助手-Setup-0.8.2.0.exe`，并附安装说明
- [ ] 已知会杀软误报，准备好放行截图/白名单说明
- [ ] 记录试点店铺 ID，用于灰度开关

### 灰度与回滚

单店关闭（推荐先这样观察）：

```json
// %LOCALAPPDATA%\Programs\PddBridgeAgent\bridge_config.json
{
  "immediate_ingress_disabled_shop_ids": ["cs_447787481:184712850"],
  "command_sender_disabled_shop_ids": []
}
```

整机回滚（改完重启桥接助手即可，无需重装）：

```json
{
  "immediate_ingress_enabled": false,
  "command_sender_enabled": false
}
```

> 两个开关都是「字符串也认」的布尔：`false / "false" / 0 / "off"` 都等于关。
> 缺失键 = 开（owner 决定的默认全开）。

## 5. 交付验收清单（30 分钟）

1. 新机器静默安装 → 桌面/开始菜单快捷方式存在 → 首启无崩溃。
2. 重启拼多多工作台 → 日志出现 `READY 1`（或 `直发注入 ... ok=True` 且后续 `send accepted` 走 DLL）。
3. 中心下发一条发送指令 → 日志 `send done ... status=confirmed real_send=True`；
   同一时刻日志出现 `self reply published locally`（**0.8.2 的验收点**）。
4. 浮窗倒计时：AI 回复后 1–2 秒内停止，不再走到 90s。
5. 买家发一条消息 → 本地工作台 2 秒内出现，中心 `center_accepted`。
6. 覆盖升级 0.7.x → 0.8.2：`bridge_config.json`/队列/日志保留，旧进程被杀掉，
   `Get-Process LocalSeatGateway | Select StartTime` 显示已重启。
7. 卸载 → 重装，确认没有旧进程占用导致覆盖失败。
8. 关闭 `command_sender_enabled` 重启，确认指令仍能发送（回落串行路径）。

## 6. 不在本版范围

- 原生接收全量 hook（当前只收心跳/出口帧）
- HTTPS/WSS 传输与令牌加密存储
- 代码签名
- 20 店以上的容量验证（本次只在 1 店环境跑过）
- 老测试用例的修复（26 failed，独立处理）
