# -*- coding: utf-8 -*-
"""自测：离线套件 + 隔离压测 + 现网只读体检（不注入假消息、不发真实消息）。

跑法:
    python selfcheck.py            # 三层全跑
    python selfcheck.py --quick    # 跳过单测与隔离压测, 只做现网只读体检
    python selfcheck.py --live-dir "D:\\...\\PddBridgeAgent"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED_VERSION = None  # 从 bridge/__init__.py 读取

RESULTS: list[tuple[str, str, str]] = []   # (level, name, detail)


def record(level: str, name: str, detail: str = "") -> None:
    RESULTS.append((level, name, detail))
    print("[%s] %-46s %s" % (level, name, detail))


def run(cmd, timeout=1800, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"     # 子进程输出统一 utf-8, 否则中文管道下是 GBK
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=str(cwd or ROOT), env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def python_exe() -> str:
    # 用装了依赖的那个解释器（和发布构建一致）
    for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python310/python.exe",
                 Path(sys.executable)):
        if cand.exists():
            return str(cand)
    return sys.executable


PY = python_exe()


# ---------------------------------------------------------------- 1. 离线套件
def check_unit_tests() -> None:
    code, out = run([PY, "-m", "pytest", "-q", *[p.name for p in sorted(ROOT.glob("test_*.py"))],
                     "tests"], timeout=1800)
    tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
    passed = re.search(r"(\d+) passed", out)
    failed = re.search(r"(\d+) failed", out)
    names = re.findall(r"^FAILED ([^\s]+)", out, re.M)
    if code == 0 and passed and not failed:
        record("OK", "单元/回归测试", tail[0].strip())
    else:
        record("FAIL", "单元/回归测试", (tail[0].strip() + (" | " + ", ".join(names[:5]) if names else ""))[:200])


def check_inject_js() -> None:
    code, out = run(["node", str(ROOT / "tests" / "inject_push_check.js")], timeout=300)
    ok = code == 0 and "OK" in out
    detail = next((l for l in out.splitlines() if "连续同长消息" in l), "")
    record("OK" if ok else "FAIL", "注入层 JS 行为(推送/缓冲/连续同长)", detail[:120])
    if not ok:
        print(out[-600:])


def check_consecutive_ab() -> None:
    """用备份里的旧脚本做 A/B：旧实现必须复现丢消息, 新实现必须 6/6。"""
    backups = sorted(Path("D:/temp/pdd-桥接助手安装版").glob("PddBridgeAgent.bak-*/_internal/bridge/pddbridge/inject.js"))
    if not backups:
        record("WARN", "连续消息 A/B（旧脚本对照）", "未找到旧脚本备份, 跳过")
        return
    script = r'''
const fs=require('fs'), vm=require('vm');
function load(src){
  const win={pinnotification:{message:function(){}}};
  function WS(){} WS.prototype={addEventListener:function(){},onmessage:null};
  const sb={window:win,document:{title:'t'},navigator:{},console:console,
    fetch:function(){return Promise.resolve({ok:true});},WebSocket:WS,Blob:function(){},
    ArrayBuffer:function(){},Promise:Promise,JSON:JSON,Date:Date,Object:Object,Array:Array,
    String:String,setTimeout:setTimeout};
  vm.createContext(sb); vm.runInContext(src,sb); return win;
}
function frame(i){return '{"response":"push","message":{"from":{"role":"user","uid":"4764375385604",'
 + '"csid":"cs_12345:678"},"to":{"role":"mall_cs"},"msg_id":"17896333168'+i+'","content":"消息'+i
 + '","type":0,"ts":1756000000000}}';}
const frames=[1,2,3,4,5,6].map(frame);
const out={};
for (const [name,path] of [[ 'old', process.argv[2] ], [ 'new', process.argv[3] ]]){
  const win=load(fs.readFileSync(path,'utf8'));
  win.__pddBridge_push_url='';
  frames.forEach(f=>win.pinnotification.message('socket_message', f));
  out[name]=win.__pddBridge_buf.length;
}
console.log(JSON.stringify(out));
'''
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "selfcheck_ab.js"
    tmp.write_text(script, encoding="utf-8")
    code, out = run(["node", str(tmp), str(backups[-1]),
                     str(ROOT / "bridge" / "pddbridge" / "inject.js")], timeout=300)
    m = re.search(r'\{"old":(\d+),"new":(\d+)\}', out)
    if code == 0 and m:
        old, new = int(m.group(1)), int(m.group(2))
        if new == 6 and old < 6:
            record("OK", "连续消息 A/B（旧脚本对照）", "旧=%d/6 丢, 新=%d/6 全留下" % (old, new))
        else:
            record("FAIL", "连续消息 A/B（旧脚本对照）", "old=%d new=%d（应 old<6, new=6）" % (old, new))
    else:
        record("FAIL", "连续消息 A/B（旧脚本对照）", out.strip()[-120:])


# ---------------------------------------------------------------- 2. 隔离压测
def check_isolated_load() -> None:
    numbers = {}
    for transport in ("push", "drain"):
        code, out = run([PY, str(ROOT / "load_sim_cdp.py"), "--transport", transport,
                         "--rate", "600", "--minutes", "0.2"], timeout=900)
        m = re.search(r"页面→本机管线: p50=([\d.]+)ms.*?max=([\d.]+)ms", out)
        loss = re.search(r"链路计数: 页面生成 (\d+) → 本机管线 (\d+) → 中心收到 (\d+)", out)
        drops = re.search(r"丢弃计数: (\{[^}]*\})", out)
        if code != 0 or not m or not loss:
            tail = "\n      ".join((out.strip().splitlines() or ["无输出"])[-3:])
            record("FAIL", "隔离压测(%s)" % transport, tail[:200])
            continue
        gen, pipe, center = (int(x) for x in loss.groups())
        numbers[transport] = float(m.group(1))
        if gen != pipe or pipe != center:
            record("FAIL", "隔离压测(%s)" % transport, "链路计数不一致: %s" % loss.group(0))
        elif drops and drops.group(1) not in ("{}", ""):
            record("WARN", "隔离压测(%s)" % transport, "有丢弃计数: %s" % drops.group(1)[:80])
        else:
            record("OK", "隔离压测(%s)" % transport,
                   "p50=%sms max=%sms 零丢失(%d 条)" % (m.group(1), m.group(2), gen))
    if "push" in numbers and "drain" in numbers:
        if numbers["push"] < numbers["drain"] / 2:
            record("OK", "推送 vs 轮询", "p50 %.1fms vs %.1fms（%.0f×）"
                   % (numbers["push"], numbers["drain"], numbers["drain"] / max(0.001, numbers["push"])))
        else:
            record("WARN", "推送 vs 轮询", "差距不明显: %.1f vs %.1f" % (numbers["push"], numbers["drain"]))


# ---------------------------------------------------------------- 3. 现网体检
def read_log(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_live_dir(explicit: str = "") -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["name", "exe"]):
        if (proc.info.get("name") or "").lower() == "pddbridgeagent.exe":
            exe = proc.info.get("exe")
            if exe:
                return Path(exe).parent
    return None


def check_live(live_dir: Path | None) -> None:
    if live_dir is None:
        record("WARN", "现网体检", "没找到正在跑的 PddBridgeAgent.exe（传 --live-dir 指定）")
        return
    record("OK", "现网安装目录", str(live_dir))

    # 3.1 心跳还在跳
    log = live_dir / "logs" / "bridge-pipeline.log"
    lines = read_log(log)
    if not lines:
        record("FAIL", "现网日志", "读不到 %s" % log)
        return
    stamps = [l[:19] for l in lines if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", l)]
    try:
        last = time.mktime(time.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S"))
        age = time.time() - last
        record("OK" if age < 30 else "WARN", "心跳时效", "最后一行 %.0f 秒前" % age)
    except Exception:
        record("WARN", "心跳时效", "无法解析时间戳")

    # 3.2 版本与关键启动行
    starts = [i for i, l in enumerate(lines) if "桥接助手" in l and "启动" in l]
    if not starts:
        record("FAIL", "现网版本", "日志里没有启动行")
        return
    seg = lines[starts[-1]:]
    ver = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*启动", seg[0]) or re.search(r"(\d+\.\d+\.\d+\.\d+)", seg[0])
    version = ver.group(1) if ver else "?"
    lvl = "OK" if (EXPECTED_VERSION and version == EXPECTED_VERSION) else "WARN"
    record(lvl, "现网版本", "%s（代码版本 %s）" % (version, EXPECTED_VERSION))
    push_port = next((re.search(r"127\.0\.0\.1:(\d+)", l).group(1)
                      for l in seg if "推送通道监听" in l), None)
    record("OK" if push_port else "WARN", "推送通道", ("端口 %s" % push_port) if push_port else "没启动")
    sessions = [l for l in seg if "会话已挂" in l]
    record("OK" if sessions else "FAIL", "注入会话",
           "%d 个: %s" % (len(sessions), ", ".join(
               (re.search(r"account=(\S+)", l).group(1) if re.search(r"account=(\S+)", l) else "?")
               for l in sessions)) if sessions else "没有任何会话挂上")

    # 3.3 段计数
    cnt = {k: sum(1 for l in seg if p in l) for k, p in (
        ("queued", "event queued"), ("terminal", "event upload terminal"),
        ("dedup", "duplicate skipped"), ("refused", "upload refused"),
        ("would_drop", "本会误丢"), ("warn", "[WARNING]"))}
    record("OK" if cnt["terminal"] >= cnt["queued"] - 2 else "WARN", "上报吞吐",
           "queued=%d terminal=%d dedup=%d refused=%d" % (cnt["queued"], cnt["terminal"],
                                                          cnt["dedup"], cnt["refused"]))
    record("OK", "旧包装误丢(已拦下)", "本段累计 %d 次告警" % cnt["would_drop"])

    # 3.4 真正的丢弃
    hard = [l for l in seg if re.search(r"注入丢失|缓冲溢出|推送队列满|全部会话|未注入\(这些店铺", l)]
    record("OK" if not hard else "FAIL", "硬丢弃事件",
           "无" if not hard else hard[-1][:120])

    # 3.5 队列与 pending
    pend = [re.search(r"pending=(\d+).*oldest_wait=([\d.]+)s", l) for l in seg if "event queue pending=" in l]
    pend = [m for m in pend if m]
    if pend:
        pending = max(int(m.group(1)) for m in pend)
        oldest = max(float(m.group(2)) for m in pend)
        record("OK" if pending == 0 and oldest < 5 else "WARN", "队列积压",
               "峰值 pending=%d oldest_wait=%.2fs" % (pending, oldest))

    # 3.6 归档与死信
    raw = live_dir / (Path(live_dir / "bridge_queue_pdd.jsonl").stem + "_frames_raw.jsonl")
    refused = live_dir / (Path(live_dir / "bridge_queue_pdd.jsonl").stem + "_refused.jsonl")
    record("OK" if raw.exists() else "WARN", "原始帧归档",
           "%s（%d 行）" % (raw.name, len(read_log(raw))) if raw.exists() else "尚未生成(还没出现异常帧)")
    if refused.exists():
        reasons: dict[str, int] = {}
        for line in read_log(refused):
            try:
                r = json.loads(line).get("refused_reason") or "?"
            except Exception:
                r = "<解析失败>"
            reasons[r] = reasons.get(r, 0) + 1
        record("WARN" if reasons.get("invalid_message") else "OK", "中心拒收死信",
               json.dumps(reasons, ensure_ascii=False))

    # 3.7 页面侧（只读）
    try:
        sys.path.insert(0, str(ROOT))
        from bridge.pddbridge import cdp as pdd_cdp
        # CDP 调试端口在“会话已挂 port=xxxx”里; 推送端口是另一个, 不能混用
        cdp_port = None
        for line in reversed(seg):
            hit = re.search(r"会话已挂 port=(\d+)", line)
            if hit:
                cdp_port = int(hit.group(1))
                break
        ports = [cdp_port] if cdp_port else sorted(pdd_cdp.discover_ports(only_pdd=True))
        for target_port in ports[:1]:
            if not pdd_cdp.is_alive(target_port):
                record("WARN", "页面注入", "调试端口 %s 不可达" % target_port)
                break
            for target in pdd_cdp.find_chat_targets(target_port):
                conn = pdd_cdp.Cdp(target["webSocketDebuggerUrl"])
                try:
                    conn.enable_runtime()
                    res = conn.eval(
                        "({hooked:!!window.__pddBridge_hooked, guard:!!(window.__pddBridge_dedup&&"
                        "window.__pddBridge_dedup.__pddBridgeGuard), blocked:(window.__pddBridge_dedup&&"
                        "window.__pddBridge_dedup._blocked)||0, push:!!window.__pddBridge_push_url,"
                        " stats:(window.__pddBridge_stats?window.__pddBridge_stats():null)})", 1)
                finally:
                    conn.close()
                if not isinstance(res, dict) or not res.get("hooked"):
                    continue
                stats = res.get("stats") or {}
                record("OK" if res.get("guard") else "WARN", "页面判重守卫",
                       "已装=%s 本会误丢(已拦下)=%s" % (res.get("guard"), res.get("blocked")))
                record("OK" if res.get("push") else "WARN", "页面推送接管",
                       "push_url=%s" % ("已下发" if res.get("push") else "未下发(页面还没刷新, 走 200ms 轮询)"))
                overflow = int(stats.get("overflow_drop") or 0)
                record("OK" if overflow == 0 else "WARN", "页面缓冲溢出",
                       "overflow_drop=%s buf=%s recorded=%s drained=%s"
                       % (overflow, stats.get("buf"), stats.get("recorded"), stats.get("drained")))
                break
    except Exception as exc:
        record("WARN", "页面注入", "检查失败: %s" % str(exc)[:80])

    # 3.8 进程资源
    try:
        import psutil
        for proc in psutil.process_iter(["name", "memory_info", "create_time"]):
            if (proc.info.get("name") or "").lower() == "pddbridgeagent.exe":
                info = proc.info
                cpu = proc.cpu_times().user + proc.cpu_times().system
                up = time.time() - (info["create_time"] or time.time())
                record("OK", "进程资源", "内存 %.0fMB CPU %.0fs / 运行 %.0fs（%.1f%% 单核）"
                       % (info["memory_info"].rss / 1048576, cpu, up, cpu * 100 / max(1, up)))
                break
    except Exception:
        pass


def main() -> int:
    global EXPECTED_VERSION
    parser = argparse.ArgumentParser(description="0.5.19 桥接自测")
    parser.add_argument("--quick", action="store_true", help="只做现网只读体检")
    parser.add_argument("--live-dir", default="", help="现网安装目录")
    args = parser.parse_args()

    src = (ROOT / "bridge" / "__init__.py").read_text(encoding="utf-8")
    EXPECTED_VERSION = (re.search(r'__version__\s*=\s*"([^"]+)"', src) or [None, "?"])[1]

    print("=" * 78)
    print("PddBridgeAgent 自测  代码版本=%s  解释器=%s" % (EXPECTED_VERSION, PY))
    print("=" * 78)
    if not args.quick:
        check_unit_tests()
        check_inject_js()
        check_consecutive_ab()
        check_isolated_load()
    check_live(find_live_dir(args.live_dir))

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print("-" * 78)
    print("汇总: OK=%d WARN=%d FAIL=%d" % (len(RESULTS) - len(fails) - len(warns), len(warns), len(fails)))
    for level, name, detail in fails:
        print("  FAIL %s — %s" % (name, detail))
    for level, name, detail in warns:
        print("  WARN %s — %s" % (name, detail))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
