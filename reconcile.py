# -*- coding: utf-8 -*-
"""投递对账：回答「工作台能看到、大脑却没有」的每条消息到底卡在哪。

读投递台账（*_ledger.jsonl），按 event_id 还原每条消息的状态链：
    采集入队 → 本地工作台(127.0.0.1:18767) → 中心(大脑)

跑法:
    python reconcile.py                      # 最近 120 分钟
    python reconcile.py --minutes 1440       # 最近一天
    python reconcile.py --live-dir "D:\\...\\PddBridgeAgent"
    python reconcile.py --details 20         # 差异明细条数
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge.ledger import read_rows, summarize  # noqa: E402


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
            if proc.info.get("exe"):
                return Path(proc.info["exe"]).parent
    return None


def ledger_path_for(live_dir: Path, explicit: str = "") -> Path | None:
    if explicit:
        return Path(explicit)
    for candidate in sorted(live_dir.glob("*_ledger.jsonl")) + [live_dir / "bridge_queue_pdd_ledger.jsonl"]:
        if candidate.is_file():
            return candidate
    return None


def fmt_time(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def reached_brain(item: dict) -> bool:
    """到达大脑 = 中心明确 accepted, 或 拿到终态且没被拒/忽略。"""
    states = item["states"]
    if "center_accepted" in states:
        return True
    if "center_terminal" in states and not ({"center_refused", "center_ignored"} & set(states)):
        return True
    return False


def verdict(summary: dict) -> list[str]:
    """口径: 只有「入队过(captured)」的消息才算分母; 未到大脑的逐条归因。"""
    events = summary["events"]
    captured = {eid: item for eid, item in events.items() if "captured" in item["states"]}
    total = len(captured)
    accepted = [i for i in captured.values() if reached_brain(i)]
    ignored = [i for i in captured.values() if "center_ignored" in i["states"]
               and "center_accepted" not in i["states"]]
    refused = [i for i in captured.values() if "center_refused" in i["states"]
               and "center_accepted" not in i["states"] and "center_ignored" not in i["states"]]
    no_result = [i for i in captured.values()
                 if not any(k in i["states"] for k in ("center_accepted", "center_terminal",
                                                       "center_ignored", "center_refused"))]
    local_failed = [i for i in captured.values() if "local_fail" in i["states"]]
    missing = total - len(accepted)
    lines = [
        "采集入队(可核对)  %d 条" % total,
        "到达大脑          %d 条（%.1f%%）" % (len(accepted), len(accepted) * 100.0 / max(1, total)),
        "没到大脑          %d 条" % missing,
    ]
    if missing <= 0:
        lines.append("  → 本时间窗内没有「工作台有、大脑没有」的消息。")
        return lines
    lines += [
        "  · 中心明确拒收     %d 条  ← 中心拒收（内容过滤/入参校验），客户端已存死信可补推" % len(refused),
        "  · 大脑主动忽略     %d 条  ← 中心收到了但选择不处理" % len(ignored),
        "  · 无中心结果       %d 条  ← 客户端侧问题（没上报成功 / 还在重试）" % len(no_result),
        "  · 本地工作台也失败 %d 条  ← 连本机投递都没成功" % len(local_failed),
    ]
    return lines


def rows_from_pipeline_log(path: Path, since: float) -> list[dict]:
    """从 logs/bridge-pipeline.log 回溯台账（装台账之前的消息也能对账）。

    能还原三段: 入队(captured) / 中心结果(accepted/ignored/refused) / 重复跳过。
    日志里没有买家/账号/内容, 但计数和归因够用。
    """
    rows: list[dict] = []
    if not path.is_file():
        return rows
    queued = re.compile(r"event queued id=(\S+) msg_id=(\S+) ts=(\S+) captured_at_ms=(\d+) enqueued_at=([\d.]+)")
    terminal = re.compile(r"event upload terminal=\d+ remaining=\d+ terminal_ids=\[([^\]]*)\]")
    refused = re.compile(r"event upload refused.*?ids=\[([^\]]*)\]")
    skipped = re.compile(r"event duplicate skipped id=(\S+)")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows

    def stamp(line: str) -> float:
        try:
            return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return 0.0

    for line in lines:
        at = stamp(line)
        if at and at < since:
            continue
        hit = queued.search(line)
        if hit:
            rows.append({"at": float(hit.group(5)), "event_id": hit.group(1), "state": "captured",
                         "msg_id": hit.group(2), "source": "log"})
            continue
        hit = terminal.search(line)
        if hit:
            for event_id in re.findall(r"'([^']+)'", hit.group(1)):
                rows.append({"at": at, "event_id": event_id, "state": "center_terminal",
                             "source": "log"})
            continue
        hit = refused.search(line)
        if hit:
            reason_match = re.search(r"reasons=\[([^\]]*)\]", line)
            reasons = re.findall(r"'([^']+)'", reason_match.group(1)) if reason_match else []
            reason = reasons[0] if reasons else "?"
            state = "center_ignored" if reason in ("ignored", "plugin_send_echo") else "center_refused"
            for event_id in re.findall(r"'([^']+)'", hit.group(1)):
                rows.append({"at": at, "event_id": event_id, "state": state,
                             "detail": reason, "source": "log"})
            continue
        hit = skipped.search(line)
        if hit:
            rows.append({"at": at, "event_id": hit.group(1), "state": "center_duplicate_skipped",
                         "source": "log"})
    return rows


def merge_rows(primary: list[dict], extra: list[dict]) -> list[dict]:
    """台账优先: 同一 (event_id, state) 已有台账行就不再用日志回溯行。"""
    seen = {(str(r.get("event_id") or ""), str(r.get("state") or "")) for r in primary}
    merged = list(primary)
    for row in extra:
        key = (str(row.get("event_id") or ""), str(row.get("state") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda r: float(r.get("at") or 0.0))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="投递对账（工作台 vs 大脑）")
    parser.add_argument("--minutes", type=float, default=120.0, help="回看时间窗（分钟）")
    parser.add_argument("--live-dir", default="", help="现网安装目录")
    parser.add_argument("--ledger", default="", help="台账文件路径")
    parser.add_argument("--details", type=int, default=12, help="差异明细条数")
    parser.add_argument("--no-backfill", action="store_true",
                        help="不用 pipeline 日志回溯（只看台账本身）")
    args = parser.parse_args()

    live_dir = find_live_dir(args.live_dir)
    path = ledger_path_for(live_dir, args.ledger) if live_dir or args.ledger else None
    if path is None or not path.exists():
        print("找不到投递台账（*_ledger.jsonl）。需要先装 0.5.19.14+ 并跑一段时间。")
        print("现网目录: %s" % (live_dir or "未找到"))
        return 2

    since = time.time() - max(1.0, args.minutes) * 60.0
    rows = read_rows(path)
    source_note = "台账"
    if not args.no_backfill and live_dir is not None:
        back = rows_from_pipeline_log(live_dir / "logs" / "bridge-pipeline.log", since)
        if back:
            rows = merge_rows(rows, back)
            source_note = "台账 + 日志回溯"
    summary = summarize(rows, since=since)
    events = summary["events"]

    print("=" * 84)
    print("投递对账  数据源=%s  (%s)" % (source_note, path))
    print("时间窗: 最近 %.0f 分钟（%s → %s）"
          % (args.minutes, fmt_time(since), fmt_time(time.time())))
    print("-" * 84)
    for line in verdict(summary):
        print(line)

    # 差异明细：工作台收到了、但大脑没有最终结果
    gap_items = [item for item in events.values()
                 if "captured" in item["states"] and not reached_brain(item)]
    gap_items.sort(key=lambda x: x["at"])
    if gap_items:
        print("-" * 84)
        print("差异明细（最近 %d 条，按时间）:" % min(args.details, len(gap_items)))
        reasons = Counter()
        for item in gap_items[-args.details:]:
            states = item["states"]
            if "center_refused" in states:
                state = "中心拒收"
            elif "center_ignored" in states:
                state = "大脑忽略"
            elif "center_retry" in states:
                state = "重试中"
            elif "local_fail" in states:
                state = "工作台也失败"
            else:
                state = "未上报/无结果"
            reasons[state] += 1
            local = "工作台√" if "local_ok" in states else ("工作台×" if "local_fail" in states else "工作台?")
            print("  %s  %-9s %s  买家=%s  账号=%s  %s"
                  % (fmt_time(item["at"]), state, local, item["buyer_id"] or "-",
                     item["account"] or "-", (item["content"] or "")[:26]))
            if item["detail"]:
                print("        原因: %s" % item["detail"][:110])
        print("  明细归类: %s" % json.dumps(dict(reasons), ensure_ascii=False))

    dead = live_dir / "bridge_queue_pdd_refused.jsonl" if live_dir else None
    if dead and dead.exists():
        n = sum(1 for line in dead.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        print("-" * 84)
        print("死信文件 %s: %d 条（可补推）" % (dead.name, n))
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
