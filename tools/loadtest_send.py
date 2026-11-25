# -*- coding: utf-8 -*-
"""单店连发压测：直接驱动 PddbridgeSource 的页面发送通道，测每条的真实耗时。

用法（先停掉桥接助手，避免两个进程抢页面里的 __pddBridge_* 全局）：

    py -3 tools/loadtest_send.py --buyer 4764375385604 \
        --account cs_447787481:184712850 --count 10

输出：每条 status / via / 耗时，以及 p50 / p95 / max 和 confirmed 命中率。
注意：这是**真实发送**，只对测试买家号用。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import load_config          # noqa: E402
from bridge.pddbridge_source import PddbridgeSource  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buyer", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--gap", type=float, default=0.5, help="两条之间间隔秒数")
    ap.add_argument("--config", default=os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\PddBridgeAgent\bridge_config.json"))
    ap.add_argument("--text", default="压测连发")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    cfg["data_source"] = "cdp"
    src = PddbridgeSource(cfg, on_event=lambda e: None).start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            st = src.status()
            if st.get("state") == "listening":
                break
            time.sleep(1)
        st = src.status()
        print("state=%s send_ready=%s sessions=%s" % (
            st.get("state"), st.get("send_ready"), st.get("sessions")), flush=True)
        if st.get("state") != "listening":
            print("CDP 未就绪，压测中止")
            return 2

        rows = []
        for i in range(1, args.count + 1):
            text = "%s #%d/%d %s" % (args.text, i, args.count,
                                      time.strftime("%H:%M:%S"))
            t0 = time.monotonic()
            result = src.send_and_wait(args.buyer, text, args.account,
                                       timeout=args.timeout)
            cost = time.monotonic() - t0
            rows.append({"i": i, "cost": cost,
                         "status": str(result.get("status")),
                         "real_send": result.get("real_send"),
                         "via": str(result.get("via")),
                         "err": str(result.get("error") or result.get("error_user") or "")[:60]})
            print("#%-2d %5.2fs status=%-12s real_send=%-5s via=%-12s %s" % (
                i, cost, rows[-1]["status"], rows[-1]["real_send"],
                rows[-1]["via"], rows[-1]["err"]), flush=True)
            if args.gap:
                time.sleep(args.gap)

        costs = sorted(r["cost"] for r in rows)
        confirmed = sum(1 for r in rows if r["status"] == "confirmed")
        accepted = sum(1 for r in rows if r["status"] == "accepted")
        failed = sum(1 for r in rows if r["status"] in {"failed", "blocked", "indeterminate"})
        p50 = statistics.median(costs)
        p95 = costs[min(len(costs) - 1, int(round(0.95 * (len(costs) - 1))))]
        print("\n=== 汇总 ===")
        print("n=%d confirmed=%d accepted=%d failed/blocked=%d" % (
            len(rows), confirmed, accepted, failed))
        print("p50=%.2fs p95=%.2fs max=%.2fs min=%.2fs" % (
            p50, p95, max(costs), min(costs)))
        print("send_count=%s direct_fallback=%s" % (
            getattr(src, "send_count", "?"), getattr(src, "direct_fallback_count", "?")))
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0 if failed == 0 else 1
    finally:
        src.stop()


if __name__ == "__main__":
    raise SystemExit(main())
