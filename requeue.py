# -*- coding: utf-8 -*-
"""死信补推：把 *_refused.jsonl 里被中心拒收/忽略的事件重新投给中心。

默认是**预览**（不发任何请求）。确认要发再加 --apply。

跑法:
    python requeue.py                                  # 预览：会补推哪些
    python requeue.py --apply                          # 真补推（默认排除诊断帧）
    python requeue.py --apply --reason invalid_message # 只补某一类原因
    python requeue.py --apply --include-diagnostics    # 连诊断帧一起补
    python requeue.py --apply --keep-history           # 补推后仍保留原死信文件

行为:
- 按 reasons 分组，默认排除 is_diagnostic 的诊断帧（中心现在还只接买家消息，补了也白补）；
- 每批 ≤100 条，走和客户端完全相同的接口 /api/bridge/v1/events；
- 中心接收的从死信文件里删掉，仍被拒的保留并记录最新原因（失败不会丢）；
- 结果写进投递台账（requeued / center_accepted / center_refused），可直接用 reconcile.py 复核。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge.client import BridgeClient, BridgeClientError  # noqa: E402
from bridge.ledger import DeliveryLedger  # noqa: E402

BATCH = 100
# 中心只接买家消息；这些原因补了也是再被拒一次
DIAGNOSTIC_REASONS = {"unsupported_role"}


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


def load_rows(path: Path) -> list[dict]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event_id"):
            rows.append(row)
    return rows


def select_rows(rows: list[dict], *, reason: str = "", include_diagnostics: bool = False,
                limit: int = 0) -> list[dict]:
    picked = []
    seen: set[str] = set()
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if event_id in seen:
            continue                  # 死信里同一事件可能有多行（上传在 ack 前重发过）
        why = str(row.get("refused_reason") or "")
        if reason and why != reason:
            continue
        if not include_diagnostics:
            if row.get("is_diagnostic") or why in DIAGNOSTIC_REASONS:
                continue
        seen.add(event_id)
        picked.append(row)
    return picked[:limit] if limit else picked


def clean_event(row: dict) -> dict:
    """去掉本地字段，只把事件本身发回中心。"""
    event = {k: v for k, v in row.items()
             if k not in ("refused_reason", "requeued_at", "requeue_error", "refused_count")}
    return event


def requeue(rows: list[dict], client, *, apply: bool = False,
            ledger: DeliveryLedger | None = None) -> dict:
    """核心逻辑（可注入假 client 测试）。返回 {sent, accepted, ignored, refused, outcomes}。"""
    result = {"sent": 0, "accepted": 0, "ignored": 0, "refused": 0,
              "outcomes": {}, "errors": []}
    if not apply or not rows:
        return result
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        events = [clean_event(row) for row in chunk]
        try:
            response = client.upload_events(events)
        except BridgeClientError as exc:
            result["errors"].append(str(exc)[:200])
            for row in chunk:
                result["outcomes"][row["event_id"]] = ("error", str(exc)[:120])
            continue
        acks = response.get("event_acks") if isinstance(response, dict) else None
        acks = acks if isinstance(acks, list) else []
        by_id = {str(a.get("event_id") or ""): a for a in acks if isinstance(a, dict)}
        for row in chunk:
            event_id = str(row["event_id"])
            ack = by_id.get(event_id)
            if ack is None:
                result["outcomes"][event_id] = ("no_ack", "")
                continue
            status = str(ack.get("status") or "")
            reason = str(ack.get("reason")
                         or (ack.get("error") if isinstance(ack.get("error"), dict) else {}).get("code")
                         or status)
            result["sent"] += 1
            if status == "accepted" or ack.get("committed") is True:
                result["accepted"] += 1
                result["outcomes"][event_id] = ("accepted", reason)
            elif status == "ignored":
                result["ignored"] += 1
                result["outcomes"][event_id] = ("ignored", reason)
            else:
                result["refused"] += 1
                result["outcomes"][event_id] = ("refused", reason)
            if ledger is not None:
                ledger.record(event_id, "requeued", event=clean_event(row))
                if status == "accepted" or ack.get("committed") is True:
                    ledger.record(event_id, "center_accepted", event=clean_event(row))
                elif status == "ignored":
                    ledger.record(event_id, "center_ignored", event=clean_event(row), detail=reason)
                else:
                    ledger.record(event_id, "center_refused", event=clean_event(row), detail=reason)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="死信补推（默认只预览）")
    parser.add_argument("--apply", action="store_true", help="真正发送（否则只预览）")
    parser.add_argument("--reason", default="", help="只补某一类原因，例如 invalid_message")
    parser.add_argument("--include-diagnostics", action="store_true",
                        help="连诊断帧(unsupported_role)一起补")
    parser.add_argument("--limit", type=int, default=0, help="最多补多少条")
    parser.add_argument("--keep-history", action="store_true", help="成功后仍保留原死信文件")
    parser.add_argument("--live-dir", default="", help="现网安装目录")
    args = parser.parse_args()

    live_dir = find_live_dir(args.live_dir)
    if live_dir is None:
        print("找不到现网安装目录（用 --live-dir 指定）")
        return 2
    dead = live_dir / "bridge_queue_pdd_refused.jsonl"
    cfg_path = live_dir / "bridge_config.json"
    if not dead.is_file():
        print("没有死信文件：%s" % dead)
        return 0
    rows = load_rows(dead)
    picked = select_rows(rows, reason=args.reason,
                         include_diagnostics=args.include_diagnostics, limit=args.limit)

    print("=" * 78)
    print("死信补推  %s" % dead)
    print("死信总数 %d 条；本次选中 %d 条%s"
          % (len(rows), len(picked), "（仅预览）" if not args.apply else ""))
    if rows:
        print("按原因: %s" % json.dumps(
            dict(Counter(str(r.get("refused_reason") or "?") for r in rows)), ensure_ascii=False))
    if picked:
        by_reason = defaultdict(int)
        for row in picked:
            by_reason[str(row.get("refused_reason") or "?")] += 1
        print("本次按原因: %s" % json.dumps(dict(by_reason), ensure_ascii=False))
        for row in picked[:8]:
            content = str(row.get("content") or "")
            print("   %s  买家=%s  %s  %s"
                  % (time.strftime("%m-%d %H:%M", time.localtime(float(row.get("ts") or 0))),
                     row.get("buyer_id") or "-", (row.get("refused_reason") or "?")[:22],
                     ("内容: " + content[:24]) if content and not row.get("is_diagnostic") else ""))
        if len(picked) > 8:
            print("   … 其余 %d 条" % (len(picked) - 8))
    if not args.apply:
        print("-" * 78)
        print("预览模式，未发送。确认后执行：")
        print("  python %s --apply%s%s" % (Path(__file__).name,
                                           (" --reason " + args.reason) if args.reason else "",
                                           " --include-diagnostics" if args.include_diagnostics else ""))
        print("提示：补推的是历史消息，中心可能按其自身的时效规则忽略；那是大脑的判断。")
        print("=" * 78)
        return 0

    if not picked:
        print("没有需要补推的（已经被过滤掉）")
        return 0

    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.is_file() else {}
    client = BridgeClient(str(cfg.get("server_url") or ""), str(cfg.get("agent_token") or ""),
                          str(cfg.get("agent_id") or ""), str(cfg.get("agent_name") or ""),
                          str(cfg.get("device_id") or ""))
    if not client.server_url or not client.agent_token:
        print("配置里缺 server_url / agent_token，无法补推")
        return 2
    ledger = DeliveryLedger(live_dir / "bridge_queue_pdd_ledger.jsonl")

    result = requeue(picked, client, apply=True, ledger=ledger)
    print("-" * 78)
    print("补推结果: 发送 %d 条 → 中心接收 %d / 忽略 %d / 仍拒收 %d%s"
          % (result["sent"], result["accepted"], result["ignored"], result["refused"],
             ("  错误 %d 次" % len(result["errors"])) if result["errors"] else ""))
    if result["errors"]:
        for err in result["errors"][:3]:
            print("   错误: %s" % err)

    # 只把"中心仍未接收"的留在死信里；接收过的删掉，避免重复补推
    still = []
    for row in rows:
        event_id = str(row["event_id"])
        outcome = result["outcomes"].get(event_id)
        if outcome and outcome[0] == "accepted":
            continue
        if outcome and outcome[0] in ("refused", "ignored", "error", "no_ack"):
            row = {**row, "refused_reason": outcome[1] or row.get("refused_reason")}
            row["requeued_at"] = time.time()
            still.append(row)
            continue
        if not outcome:
            still.append(row)          # 本次没尝试的（如诊断帧）原样保留, 不静默删除
            continue
        still.append(row)
    if args.keep_history:
        print("保留原死信文件（--keep-history）")
    else:
        backup = dead.with_suffix(dead.suffix + ".requeued-" + time.strftime("%Y%m%d-%H%M%S"))
        dead.replace(backup)
        if still:
            with dead.open("w", encoding="utf-8") as handle:
                for row in still:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("死信文件已更新，剩余 %d 条；原件备份 %s" % (len(still), backup.name))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
