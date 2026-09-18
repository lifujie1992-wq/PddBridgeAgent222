# -*- coding: utf-8 -*-
"""探域日志(tanyu_logs)模式：高并发进线压测，用真实链路复现「忙时大面积回复延迟/不回复」。

链路（除两处桩以外全是生产代码）：
    合成 inside_*.log ──真实 LogWatcher──> 真实 local-first 管线
      ├─ 真实 LocalDeliveryQueue ──HTTP──> 本机真实网关(FrontHandler+LocalSeatState)
      └─ 真实 _pending/_flush_events ──> 桩中心（只记到达时间，不模拟大脑侧队列）
    订单上下文查询用等延迟桩（--lookup-ms）替代，因为它要占真实工作台的 CDP。

跑法:
    python load_sim_tanyu.py --rate 300 --minutes 2
    python load_sim_tanyu.py --rate 300 --burst-size 300 --burst-every 60 --minutes 2
    python load_sim_tanyu.py --rate 1200 --lookup-ms 250 --pad-bytes 4000 --minutes 1

输出各段延迟分位、积压峰值、上传吞吐，并用真实 _command_expired 判定「大脑下发的
自动回复会不会被客户端按过期拒绝」（= 客户看到的"不回复"）。
"""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge.pdd_context as pdd_context
import run_pdd_client
from bridge.agent import BridgeAgent
from bridge.message_timing import LIVE_MAX_AGE_SECONDS
from bridge.pdd_context import normalize_order_response
from run_frontend_service import FrontHandler, LocalSeatState, WEB

BUYER_ACCOUNT = "cs_427302374:164945148"
BUYER_ID = "4764375385604"
MALL_ID = "427302374"


def message_line(msg_id: str, ts: float) -> str:
    return "buyer_msg=" + json.dumps({
        "platform": "pdd",
        "msg_id": msg_id,
        "platform_message_id": msg_id,
        "account": BUYER_ACCOUNT,
        "buyer_id": BUYER_ID,
        "role": "user",
        "content": f"压测消息{msg_id}",
        "ts": ts,
    }, ensure_ascii=False)


class RecordingCenter:
    """桩中心：只记录事件到达时间。故意不做队列/限流，用来把客户端自身耗时量出来。"""

    def __init__(self, written: dict, lock: threading.Lock) -> None:
        self.written = written
        self.lock = lock
        self.arrived: dict[str, float] = {}
        self.agent_id = "sim-agent"
        self._server_clock = None

    def server_now(self) -> float:
        return time.time()

    def register(self) -> dict:
        return {}

    def heartbeat(self, _status: dict) -> dict:
        return {}

    def pull_commands(self, *, wait_seconds: float = 0.0) -> list:
        return []

    def upload_events(self, events: list) -> dict:
        now = time.time()
        for event in events:
            msg_id = str(event.get("platform_message_id") or event.get("msg_id") or "")
            if msg_id:
                self.arrived.setdefault(msg_id, now)
        return {
            "ack_version": 1,
            "event_acks": [
                {"event_id": event.get("event_id"), "status": "accepted",
                 "committed": True, "retryable": False}
                for event in events
            ],
        }

    def written_at(self, msg_id: str) -> float:
        with self.lock:
            return float(self.written.get(msg_id) or 0.0)


def build_gateway(work: Path):
    state = LocalSeatState(work / "seat_state.json")
    state.configure(backend="http://127.0.0.1:1", agent_token="sim-token", agent_id="sim-agent")
    with state.lock:
        state.active_shop_ids = {f"mall_{MALL_ID}"}
        state.shop_names = {f"mall_{MALL_ID}": "压测店铺"}
        state._save_locked()

    class Handler(FrontHandler):
        local_state = state
        ui_role = "seat"
        seat_agent_token = "sim-token"
        seat_agent_id = "sim-agent"
        backend_base = "http://127.0.0.1:1"
        web_root = WEB

        def log_message(self, _fmt, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def install_lookup_stub(lookup_ms: int) -> None:
    # 继承真实类，只把“真正查一次”换成等延迟桩 → 结果缓存/并发池是真实代码。
    real = pdd_context.PddOrderContextLookup

    class StubLookup(real):
        def _lookup_uncached(self, mall_id, buyer_id):
            time.sleep(lookup_ms / 1000.0)
            return normalize_order_response(
                mall_id,
                {"ok": True, "total": 1, "orders": [{
                    "order_id": "sim-order-1", "mall_id": MALL_ID, "created_at": 123,
                    "goods_id": "sim-goods-1", "goods_name": "压测商品",
                    "goods_price_cents": 9900, "status": "已发货",
                }]},
            )

    pdd_context.PddOrderContextLookup = StubLookup


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser(description="探域日志模式进线压测")
    parser.add_argument("--rate", type=int, default=300, help="每分钟进线条数")
    parser.add_argument("--minutes", type=float, default=2.0, help="压测时长")
    parser.add_argument("--lookup-ms", type=int, default=250, help="桩订单查询耗时(毫秒)")
    parser.add_argument("--pad-bytes", type=int, default=0, help="每条消息额外写入的日志噪音字节")
    parser.add_argument("--burst-size", type=int, default=0, help="每次突发写入条数，0=均匀")
    parser.add_argument("--burst-every", type=float, default=60.0, help="突发间隔秒")
    parser.add_argument("--drain-seconds", type=float, default=120.0, help="停止进线后等排空的时间")
    parser.add_argument("--workers", type=int, default=4, help="订单查询并发数（旧行为填 1）")
    parser.add_argument("--context-timeout", type=float, default=1.0,
                        help="单条订单查询预算秒（旧行为填 1.8）")
    parser.add_argument("--cache-seconds", type=float, default=25.0,
                        help="会话级结果缓存 TTL 秒（0=关闭，即旧行为）")
    args = parser.parse_args()

    written: dict[str, float] = {}
    lock = threading.Lock()
    work = Path(tempfile.mkdtemp(prefix="load-sim-"))
    log_dir = work / "tanyu_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "inside_1.log"
    log_path.touch()

    server = build_gateway(work)
    local_url = f"http://127.0.0.1:{server.server_port}"
    install_lookup_stub(args.lookup_ms)
    run_pdd_client.install_local_first()

    cfg = {
        "platform": "pdd",
        "server_url": "http://127.0.0.1:1",
        "agent_token": "sim-token",
        "agent_id": "sim-agent",
        "agent_name": "load-sim",
        "device_id": "device-sim",
        "data_source": "tanyu_logs",
        "cdp_auto_fallback": False,
        "tanyu_log_dir": str(log_dir),
        "local_queue_path": str(work / "bridge_queue.jsonl"),
        "command_journal_path": str(work / "bridge_commands.json"),
        "local_workbench_url": local_url,
        "dual_write_local_workbench": True,
        "parser_profile_cache_path": str(work / "parser_profile.json"),
        "poll_interval_ms": 400,
        "heartbeat_seconds": 5.0,
        "command_poll_seconds": 1.5,
        "pdd_context_workers": args.workers,
        "pdd_context_timeout_seconds": args.context_timeout,
        "pdd_context_cache_seconds": args.cache_seconds,
    }

    agent = BridgeAgent(cfg)
    agent.client = RecordingCenter(written, lock)
    sampler_stop = threading.Event()
    samples: list[dict] = []

    def sample() -> None:
        while not sampler_stop.is_set():
            try:
                local_pending = int(agent._local_delivery.status().get("pending") or 0)
            except Exception:
                local_pending = -1
            samples.append({
                "center_pending": len(agent._pending),
                "context_pending": len(agent._pdd_context_pending_ids),
                "local_pending": local_pending,
            })
            sampler_stop.wait(1.0)

    threading.Thread(target=sample, daemon=True).start()

    def runner() -> None:
        try:
            agent.run_forever()
        except Exception as exc:
            print(f"[agent 退出] {type(exc).__name__}: {exc}")

    threading.Thread(target=runner, name="agent-run", daemon=True).start()
    time.sleep(3.0)

    total = max(1, int(args.rate * args.minutes))
    burst = args.burst_size or 0
    per_line_sleep = 0.0 if burst else 60.0 / args.rate
    started = time.time()
    written_count = 0
    with log_path.open("a", encoding="utf-8") as handle:
        while written_count < total and time.time() - started < args.minutes * 60:
            batch = burst if burst else 1
            burst_started = time.time()
            for _ in range(batch):
                if written_count >= total:
                    break
                msg_id = f"sim-{written_count}"
                now = time.time()
                with lock:
                    written[msg_id] = now
                handle.write(message_line(msg_id, now) + "\n")
                if args.pad_bytes:
                    handle.write("[noise] " + ("x" * args.pad_bytes) + "\n")
                written_count += 1
            handle.flush()
            if burst:
                rest = args.burst_every - (time.time() - burst_started)
                if rest > 0:
                    time.sleep(rest)
            elif per_line_sleep:
                time.sleep(per_line_sleep)
    write_finished = time.time()

    deadline = time.time() + args.drain_seconds
    while time.time() < deadline:
        local_pending = int(agent._local_delivery.status().get("pending") or 0)
        watcher_pending = len(getattr(agent.watcher, "_pending_events", {}) or {})
        if (not agent._pending and not agent._pdd_context_pending_ids
                and not local_pending and not watcher_pending):
            break
        time.sleep(0.5)
    drained = time.time()

    sampler_stop.set()
    agent._stop.set()
    time.sleep(1.5)
    server.shutdown()
    server.server_close()

    center = agent.client
    e2e: list[float] = []
    missing = 0
    for msg_id, arrived_at in center.arrived.items():
        at = center.written_at(msg_id)
        if at:
            e2e.append(arrived_at - at)
        else:
            missing += 1
    stale = sum(1 for value in e2e if value > LIVE_MAX_AGE_SECONDS)
    peak = {key: max((s[key] for s in samples), default=0)
            for key in ("center_pending", "context_pending", "local_pending")}
    elapsed = max(0.001, write_finished - started)
    status = agent._local_delivery.status()

    # 真实判定：大脑在某条消息到达后 90 秒内下发自动回复，客户端会不会拒发。
    # 用最后一条（最年轻）和第一条（最老）两个极端，避免拿“压测结束时刻”误导。
    def expiry_probe(parent_msg_id: str) -> bool:
        return agent._command_expired({
            "id": "sim-probe",
            "type": "send_text",
            "account": BUYER_ACCOUNT,
            "buyer_id": BUYER_ID,
            "content": "压测",
            "created_at": time.time(),
            "meta": {"takeover_parent_msg_id": parent_msg_id},
        })

    newest = f"sim-{written_count - 1}"
    oldest = "sim-0"
    newest_age = time.time() - center.written_at(newest) if newest in center.arrived else 0.0
    watcher_stats = agent.watcher.diagnostics()
    emitted = int(((watcher_stats.get("sources") or {}).get("inside") or {}).get("emitted_events") or 0)

    print("=" * 74)
    print(f"进线 {written_count} 条 / {elapsed:.1f}s（目标 {args.rate}/分钟，"
          f"{'突发 %d 条/%.0fs' % (burst, args.burst_every) if burst else '均匀'}）"
          f"  订单查询桩 {args.lookup_ms}ms/{args.workers}并发  噪音 {args.pad_bytes}B/条")
    print(f"链路计数: 写入 {written_count} → watcher 发出 {emitted} → 中心收到 {len(center.arrived)}"
          f"（读日志阶段丢 {max(0, written_count - emitted)}，发出到中心丢 "
          f"{max(0, emitted - len(center.arrived))}）"
          f"  排空 {drained - write_finished:.1f}s")
    print(f"中心接收吞吐 {len(center.arrived) * 60 / elapsed:.0f}/分钟"
          f"  未匹配 {missing}")
    if e2e:
        print(f"端到端(写日志→中心收到): p50={percentile(e2e, .5):.2f}s "
              f"p95={percentile(e2e, .95):.2f}s p99={percentile(e2e, .99):.2f}s "
              f"max={max(e2e):.2f}s")
        print(f"其中 >{LIVE_MAX_AGE_SECONDS:.0f}s: {stale} 条（{stale * 100.0 / len(e2e):.0f}%）")
    print(f"积压峰值: 中心队列={peak['center_pending']} 订单查询在途={peak['context_pending']} "
          f"本地工作台队列={peak['local_pending']}；末尾本地队列残留={status.get('pending')}")
    print(f"订单查询: {json.dumps(agent._pdd_context_stats, ensure_ascii=False)}")
    print(f"watcher: {json.dumps(watcher_stats, ensure_ascii=False)[:320]}")
    print(f"本地投递 last_error={status.get('last_error')!r}  上传 last_error={agent._last_error!r}")
    print(f"过期判定: 最新一条(age={newest_age:.1f}s)={expiry_probe(newest)} "
          f"最早一条={expiry_probe(oldest)}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
