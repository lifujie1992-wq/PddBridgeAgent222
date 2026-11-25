# 90科技多多助手 1.0.0（基于 0.7.6 稳定版）

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
