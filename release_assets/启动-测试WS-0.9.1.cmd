@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  PddBridgeAgent v0.9.1   10 店铺 / WS 上行
echo ------------------------------------------------------------
echo  配置: %cd%\bridge_config.json
echo    center_ws_enabled     = true      (上行走 WebSocket)
echo    data_source           = tanyu_logs (探域日志为实时主源)
echo    history_pull_seconds  = 120        (CDP 只补历史，不上报实时)
echo.
echo  v0.9.1 相对 0.9.0 的改动（都是 10 店铺峰值调优）:
echo    upload_concurrency            3  -^> 6
echo    local_first_max_wait_seconds  1.0 -^> 0.3   峰值延迟主因
echo    CDP 会话泵: 加预算+断点续轮，一个卡住的 tab 不再拖住其余 9 个
echo    CDP 求值: 修掉断连时的忙等（原来 4 秒能空转 1100 万次 recv）
echo    只补历史时空转轮询 48 -^> 3 次/秒
echo.
echo  首次启动会自动把配置迁到 v2（只改上面两个值），
echo  并在同目录留一份 bridge_config.json.pre-v2.bak
echo.
echo  启动后判断是否正常，看日志 logs\bridge-pipeline.log:
echo    1) 搜 "中心 WS 已连接"     - WS 上行通了
echo    2) 搜 "会话已挂"           - 10 个店铺都挂上了（应该有 10 行）
echo    3) 搜 "pump" / "round_budget_hit"  - 轮转有没有被预算卡住
echo  WS 连不上会自动回落 HTTP，不影响消息进大脑。
echo ============================================================
echo.
if not exist "bridge_config.json" (
  echo [错误] 找不到 bridge_config.json
  pause
  exit /b 1
)
findstr /C:"\"agent_token\": \"\"" bridge_config.json >nul && (
  echo [错误] agent_token 还是空的, 请先填写后再启动.
  pause
  exit /b 1
)
echo 正在启动...
PddBridgeAgent.exe
echo.
echo 已退出. 若窗口一闪而过, 请看 logs\bridge-pipeline.log
pause
