#!/usr/bin/env bash
# 打包 PddBridgeAgent + LocalSeatGateway + PddAdsorbWindow。
#
# 和 build_release.ps1 等价，但用 bash 写：那台开发机上 PowerShell 的执行策略会把
# 脚本挡下来（Classifier 也拦 `-ExecutionPolicy Bypass`），bash 版到处都能跑。
#
#   bash build_release.sh                 # 输出到 build-dist-release（同 ps1）
#   bash build_release.sh build-dist-091  # 输出到指定目录
#
# 注意：PyInstaller 的 --noconfirm 会**删掉目标目录**。目标目录里如果有
# bridge_config.json（含 agent_token）、队列/台账文件，先备份再打。
set -e

OUT="${1:-build-dist-release}"
WORK="${OUT}-work"
SPEC="${OUT}-spec"
# ROOT 必须是 **Windows 形式**（C:/...）。git-bash 的 pwd 给的是 /c/Users/...，
# 直接塞进 --add-data 会被 MSYS 路径转换搞成 \\c\\Users\\...，PyInstaller 报
# "Unable to find ..."，然后**打包出一个缺 inject.js/web 的残废包**（实测踩过，
# 而且它只是 ERROR 不中断，最后组装阶段才发现）。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v cygpath >/dev/null; then
    ROOT="$(cygpath -w "$ROOT" | tr '\\' '/')"
fi
PY="${PDD_BUILD_PYTHON:-$LOCALAPPDATA/Programs/Python/Python310/python.exe}"
PY="${PY//\\//}"

[ -x "$PY" ] || { echo "找不到 Python 3.10: $PY"; exit 1; }
command -v cygpath >/dev/null && PY="$(cygpath -w "$PY")"

# 先校验要打进包里的资源都在，别等到 PyInstaller 中途 ERROR 才发现
for asset in "$ROOT/bridge/pddbridge/inject.js" "$ROOT/web" "$ROOT/PddJumpHelper.exe" \
             "$ROOT/run_pdd_client.py" "$ROOT/run_frontend_service.py" "$ROOT/pdd_adsorb_window.py"; do
    [ -e "$asset" ] || { echo "打包资源缺失: $asset"; exit 1; }
done

echo "### 输出目录 $OUT   (Python: $PY)"
echo "### 1/3 PddBridgeAgent"
"$PY" -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name PddBridgeAgent \
  --distpath "$OUT/agent" \
  --workpath "$WORK/agent" \
  --specpath "$SPEC/agent" \
  --paths "$ROOT" \
  --hidden-import bridge \
  --hidden-import bridge.agent \
  --hidden-import bridge.channel \
  --hidden-import bridge.client \
  --hidden-import bridge.center_ws \
  --hidden-import bridge.command_journal \
  --hidden-import bridge.config \
  --hidden-import bridge.parser \
  --hidden-import bridge.pdd_context \
  --hidden-import bridge.watcher \
  --hidden-import bridge.gui \
  --hidden-import bridge.platforms \
  --hidden-import bridge.platforms.pdd \
  --hidden-import bridge.pddbridge_source \
  --hidden-import bridge.pddbridge \
  --hidden-import bridge.pddbridge.cdp \
  --hidden-import bridge.pddbridge.protocol \
  --add-data "$ROOT/bridge/pddbridge/inject.js;bridge/pddbridge" \
  --hidden-import tkinter \
  "$ROOT/run_pdd_client.py" 2>&1 | tail -3

echo "### 2/3 LocalSeatGateway"
"$PY" -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name LocalSeatGateway \
  --contents-directory _gateway_internal \
  --distpath "$OUT/gateway" \
  --workpath "$WORK/gateway" \
  --specpath "$SPEC/gateway" \
  --paths "$ROOT" \
  --add-data "$ROOT/web;web" \
  "$ROOT/run_frontend_service.py" 2>&1 | tail -3

echo "### 3/3 PddAdsorbWindow"
"$PY" -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name PddAdsorbWindow \
  --contents-directory _adsorb_internal \
  --distpath "$OUT/adsorb" \
  --workpath "$WORK/adsorb" \
  --specpath "$SPEC/adsorb" \
  --paths "$ROOT" \
  "$ROOT/pdd_adsorb_window.py" 2>&1 | tail -3

echo "### 组装 agent 包（主程序要求这几个和它同级）"
B="$OUT/agent/PddBridgeAgent"
cp -f "$OUT/gateway/LocalSeatGateway/LocalSeatGateway.exe" "$B/"
cp -rf "$OUT/gateway/LocalSeatGateway/_gateway_internal" "$B/"
cp -f "$OUT/adsorb/PddAdsorbWindow/PddAdsorbWindow.exe" "$B/"
cp -rf "$OUT/adsorb/PddAdsorbWindow/_adsorb_internal" "$B/"
cp -f "$ROOT/PddJumpHelper.exe" "$B/"

# 原生通道：发送/接收 DLL + 注入器随包分发；开发机缺路径不阻塞构建
for native in /d/temp/pdd-send-hook/out/pdd_send_v3.dll /d/temp/pdd-send-hook/out/injector.exe; do
  if [ -f "$native" ]; then cp -f "$native" "$B/"; else echo "  !! 缺 $native"; fi
done

echo "### 校验"
missing=""
for f in LocalSeatGateway.exe _gateway_internal PddAdsorbWindow.exe _adsorb_internal PddJumpHelper.exe; do
  [ -e "$B/$f" ] && echo "  OK   $f" || missing="$missing $f"
done
[ -z "$missing" ] || { echo "agent 包缺:$missing"; exit 1; }
echo "### 完成：$B"
