from __future__ import annotations

import json
import threading
import time
from collections import deque

import pytest

import bridge
import bridge.agent as agent_module
import bridge.pdd_context as pdd_context
import run_pdd_client
from bridge.agent import BridgeAgent as RealBridgeAgent
from bridge.pdd_context import (
    PddOrderContextLookup,
    mall_id_from_account,
    merge_lookup_result,
    normalize_order_response,
)
from run_frontend_service import LocalSeatState, WEB

BASE_INGEST = RealBridgeAgent._on_local_event


def _order(order_id: str, mall_id: str, goods_id: str = "goods-1") -> dict:
    return {
        "order_id": order_id,
        "mall_id": mall_id,
        "created_at": 123,
        "order_status": 1,
        "goods": [{
            "goods_id": goods_id,
            "goods_name": "Product",
            "goods_thumb_url": "https://example.test/product.jpg",
            "goods_spec": "Black",
            "goods_price": 1990,
        }],
    }


def test_mall_id_comes_only_from_canonical_account() -> None:
    assert mall_id_from_account("cs_517301277:186558493") == "517301277"
    assert mall_id_from_account("cs_517301277_186558493") == "517301277"
    assert mall_id_from_account("buyer 517301277 order 186558493") == ""


def test_missing_account_is_reported_as_lookup_failure() -> None:
    lookup = PddOrderContextLookup(process_iter=lambda *_args: [])
    result = lookup.lookup({"account": "", "buyer_id": "buyer-1"})
    assert set(result) == {"local_context_lookup"}
    assert result["local_context_lookup"]["ok"] is False
    assert result["local_context_lookup"]["error"] == "account_mall_missing"


def test_single_order_is_filtered_by_mall_and_exports_product_context() -> None:
    result = normalize_order_response("517301277", {
        "ok": True,
        "total": 2,
        "orders": [
            _order("visible-wrong", "999"),
            _order("visible-right", "517301277", "goods-right"),
        ],
    })
    assert result["order_id"] == "visible-right"
    assert result["goods_id"] == "goods-right"
    assert result["order_info"]["mall_id"] == "517301277"
    assert result["order_info"]["order_count"] == 1
    assert result["local_context_lookup"] == {
        "ok": True,
        "source": "pdd_cdp_order_context",
        "order_count": 1,
    }


def test_multiple_orders_are_ambiguous_and_never_choose_one() -> None:
    result = normalize_order_response("517301277", {
        "ok": True,
        "total": 2,
        "orders": [
            _order("visible-1", "517301277"),
            _order("visible-2", "517301277"),
        ],
    })
    assert "order_id" not in result
    assert result["order_info"]["order_count"] == 2
    assert result["order_info"]["total_order_count"] == 2
    assert result["order_info"]["ambiguous"] is True
    assert [row["order_id"] for row in result["order_info"]["orders"]] == [
        "visible-1", "visible-2",
    ]


def test_confirmed_no_orders_is_distinct_from_lookup_failure() -> None:
    empty = normalize_order_response("517301277", {"ok": True, "total": 0, "orders": []})
    assert empty["order_info"] == {
        "source": "pdd_cdp_order_context",
        "context_received": True,
        "order_count": 0,
        "total_order_count": 0,
        "ambiguous": False,
        "lookup_scope": "buyer_shop_recent_orders",
        "mall_id": "517301277",
        "no_orders": True,
    }
    assert empty["local_context_lookup"]["ok"] is True

    failed = normalize_order_response("517301277", {"ok": False})
    assert set(failed) == {"local_context_lookup"}
    assert failed["local_context_lookup"]["ok"] is False
    assert "order_info" not in failed


def test_explicit_message_order_survives_empty_recent_order_window() -> None:
    message = {
        "order_id": "older-visible-order",
        "order_info": {"source": "pdd_goods_card_info", "context_received": True},
    }
    lookup = normalize_order_response("517301277", {"ok": True, "total": 0, "orders": []})
    merged = merge_lookup_result(message, lookup)
    assert merged["order_id"] == "older-visible-order"
    assert merged["order_info"]["source"] == "pdd_goods_card_info"
    assert merged["order_info"]["recent_order_lookup"]["no_orders"] is True
    assert merged["local_context_lookup"]["ok"] is True


def test_explicit_consulted_product_is_not_overwritten_by_recent_order_product() -> None:
    message = {
        "goods_id": "consulted-product",
        "goods_name": "Product currently viewed",
        "goods_url": "https://mobile.yangkeduo.com/goods.html?goods_id=consulted-product",
    }
    lookup = normalize_order_response("517301277", {
        "ok": True,
        "total": 1,
        "orders": [_order("visible-order", "517301277", "ordered-product")],
    })
    merged = merge_lookup_result(message, lookup)
    assert merged["goods_id"] == "consulted-product"
    assert merged["goods_name"] == "Product currently viewed"
    assert merged["order_id"] == "visible-order"
    assert merged["order_info"]["goods_id"] == "ordered-product"


def test_local_duplicate_is_upgraded_with_order_lookup_context(tmp_path) -> None:
    state = LocalSeatState(tmp_path / "seat.json")
    base = {
        "platform": "pdd",
        "account": "cs_517301277:186558493",
        "buyer_id": "buyer-1",
        "msg_id": "message-1",
        "role": "user",
        "content": "hello",
        "ts": 100,
    }
    state.publish(base)
    state.publish({
        **base,
        "event_id": "pdd-context-message-1",
        "order_id": "visible-order",
        "order_info": {"source": "pdd_cdp_order_context", "context_received": True},
        "local_context_lookup": {"ok": True, "source": "pdd_cdp_order_context", "order_count": 1},
    })
    stored = next(iter(state.sessions.values()))["messages"][0]
    assert stored["order_id"] == "visible-order"
    assert stored["order_info"]["source"] == "pdd_cdp_order_context"
    assert stored["local_context_lookup"]["ok"] is True


def test_pdd_cdp_does_not_also_run_the_tanyu_log_watcher() -> None:
    """CDP 生效时只能跑一条通道。

    以前两条一起跑：日志里的账号是探域的展示名（主账号 / pdd42730237415），CDP 是
    cs_商城:席位，中心按 (account, buyer_id) 建会话，同一个买家就在浮窗上变成多张卡片。
    """
    calls = []

    class Probe:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def start(self):
            calls.append(self.tag)

        def stop(self):
            calls.append("stop-" + self.tag)

    def run(effective: str) -> None:
        agent = object.__new__(RealBridgeAgent)
        agent.cfg = {"agent_token": "token", "agent_name": "test"}
        agent.platform = type("Platform", (), {"name": "pdd", "label": "PDD"})()
        agent.client = type("Client", (), {"register": lambda _self: {}})()
        agent._data_source = "cdp"
        agent._effective_source = effective
        agent.watcher = Probe("watcher")
        agent.pddbridge_source = Probe("cdp")
        agent._stop = threading.Event()
        agent._stop.set()
        agent._command_loop = lambda: None
        agent.run_forever()

    run("cdp")
    assert calls == ["cdp", "stop-watcher"]

    del calls[:]
    run("tanyu_logs")
    assert calls == ["watcher", "stop-watcher"]


@pytest.mark.parametrize("lookup_times_out", [False, True])
def test_center_event_is_persisted_but_gated_until_lookup_finishes(monkeypatch, tmp_path, lookup_times_out) -> None:
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    fast_uploaded = threading.Event()
    buyer_uploaded = threading.Event()
    constructed = []

    class FakeLookup:
        def __init__(self, **_kwargs) -> None:
            pass

        def lookup(self, _message):
            lookup_started.set()
            assert release_lookup.wait(5)
            return normalize_order_response("517301277", {
                "ok": True,
                "total": 1,
                "orders": [_order("visible-order", "517301277")],
            })

    class FakeAgent(RealBridgeAgent):
        _local_first_v0512 = False
        _on_local_event = BASE_INGEST

        def __init__(self, cfg=None):
            self.cfg = dict(cfg or {})
            self.platform = type("Platform", (), {"name": "pdd"})()
            self._stop = threading.Event()
            self._pending = deque()
            self._pending_lock = threading.Lock()
            self._local_ingest_queue = None
            self._accounts_seen = {}
            self._last_error = ""
            self.queue_path = tmp_path / "center.jsonl"
            self.uploaded = []
            constructed.append(self)

        def _flush_events(self, *, blocked_ids=None):
            with self._pending_lock:
                rows = [e for e in self._pending if e["event_id"] not in (blocked_ids or set())]
                self._pending = deque(e for e in self._pending if e["event_id"] in (blocked_ids or set()))
                self._pending_ids = {e["event_id"] for e in self._pending}
            self.uploaded.extend(rows)
            self._rewrite_local_queue()
            if any(e.get("msg_id") == "fast" for e in rows):
                fast_uploaded.set()
            if any(e.get("msg_id") == "message-1" for e in rows):
                buyer_uploaded.set()

        def _status_payload(self):
            return {}

    delivery_type = run_pdd_client.LocalDeliveryQueue
    def build_delivery(**kwargs):
        assert not getattr(constructed[-1], "_immediate_upload_started", False)
        return delivery_type(**kwargs)
    monkeypatch.setattr(run_pdd_client, "LocalDeliveryQueue", build_delivery)
    monkeypatch.setattr(pdd_context, "PddOrderContextLookup", FakeLookup)
    monkeypatch.setattr(agent_module, "BridgeAgent", FakeAgent)
    run_pdd_client.install_local_first()
    patched = agent_module.BridgeAgent({
        "agent_id": "pdd-test",
        "agent_token": "token",
        "device_id": "device",
        "config_path": str(tmp_path / "bridge_config.json"),
        "local_queue_path": str(tmp_path / "center.jsonl"),
        "local_workbench_url": "http://127.0.0.1:1",
        "tanyu_log_dir": str(tmp_path),
        "dual_write_local_workbench": False,
    })
    try:
        patched._on_local_event({
            "platform": "pdd",
            "account": "cs_517301277:186558493",
            "buyer_id": "buyer-1",
            "msg_id": "message-1",
            "role": "user",
            "content": "hello",
            "ts": time.time(),
        })
        assert lookup_started.wait(1)
        assert patched.uploaded == []
        persisted = [json.loads(line) for line in patched.queue_path.read_text(encoding="utf-8").splitlines()]
        assert len(persisted) == 1
        assert persisted[0]["content"] == "hello"

        patched._on_local_event({
            "platform": "pdd", "account": "cs_517301277:186558493", "buyer_id": "buyer-2",
            "msg_id": "fast", "role": "mall_cs", "content": "offline", "ts": time.time(),
        })
        assert fast_uploaded.wait(1)
        assert not buyer_uploaded.is_set()
        if lookup_times_out:
            with patched._pdd_context_gate_lock:
                patched._pdd_context_deadlines[persisted[0]["event_id"]] = 0
            patched._immediate_upload_event.set()
        else:
            release_lookup.set()
        assert buyer_uploaded.wait(2)
        sent = next(e for e in patched.uploaded if e["msg_id"] == "message-1")
        assert sent["captured_at"] == persisted[0]["captured_at"]
        if lookup_times_out:
            assert sent["local_context_lookup"]["error"] == "context_deadline_exceeded"
            assert not sent["order_id"]
        else:
            assert sent["order_id"] == "visible-order"
            assert sent["local_context_lookup"]["ok"] is True
            assert sent["order_info"]["source"] == "pdd_cdp_order_context"
    finally:
        release_lookup.set()
        patched._stop.set()
        local_delivery = getattr(patched, "_local_delivery", None)
        if local_delivery is not None:
            local_delivery.wakeup.set()
            local_delivery.thread.join(timeout=2)
        bridge.__version__ = "0.5.15"


def test_local_shop_names_are_available_to_the_workbench_filter(tmp_path) -> None:
    state = LocalSeatState(tmp_path / "seat.json")
    state.shop_names["mall_517301277"] = "忆捷云途专卖店"

    state.remote_cache["/api/status"] = {
        "config": {"shop_name_map": {"mall_427302374": "T3星球蓝莓"}}
    }

    assert state.merged_status()["shop_name_map"] == {
        "mall_517301277": "忆捷云途专卖店",
        "mall_427302374": "T3星球蓝莓",
    }


def test_order_lookup_caches_per_conversation(monkeypatch) -> None:
    """同一会话连发多条、日志与 CDP 两路各送一次，只能真正查一次 CDP。"""
    calls: list[tuple] = []

    class CountingLookup(PddOrderContextLookup):
        def _lookup_uncached(self, mall_id, buyer_id):
            calls.append((mall_id, buyer_id))
            return normalize_order_response(mall_id, {
                "ok": True, "total": 1, "orders": [_order("visible-order", "517301277")],
            })

    lookup = CountingLookup(timeout=0.5, cache_seconds=30.0)
    message = {"account": "cs_517301277:186558493", "buyer_id": "buyer-1"}
    first = lookup.lookup(message)
    second = lookup.lookup(message)
    assert calls == [("517301277", "buyer-1")]
    assert first == second
    lookup.lookup({**message, "buyer_id": "buyer-2"})
    assert len(calls) == 2


def test_order_lookup_cache_expires(monkeypatch) -> None:
    calls: list[int] = []

    class CountingLookup(PddOrderContextLookup):
        def _lookup_uncached(self, mall_id, buyer_id):
            calls.append(1)
            return normalize_order_response(mall_id, {
                "ok": True, "total": 1, "orders": [_order("visible-order", "517301277")],
            })

    lookup = CountingLookup(timeout=0.5, cache_seconds=0.05)
    message = {"account": "cs_517301277:186558493", "buyer_id": "buyer-1"}
    lookup.lookup(message)
    time.sleep(0.08)
    lookup.lookup(message)
    assert len(calls) == 2


def _context_agent(monkeypatch, tmp_path, lookup_cls, **cfg):
    class FakeAgent(RealBridgeAgent):
        _local_first_v0512 = False
        _on_local_event = BASE_INGEST

        def __init__(self, cfg=None):
            self.cfg = dict(cfg or {})
            self.platform = type("Platform", (), {"name": "pdd"})()
            self._stop = threading.Event()
            self._pending = deque()
            self._pending_lock = threading.Lock()
            self._local_ingest_queue = None
            self._accounts_seen = {}
            self._last_error = ""
            self.queue_path = tmp_path / "context.jsonl"

        def _flush_events(self, *, blocked_ids=None):
            return None

        def _status_payload(self):
            return {}

    monkeypatch.setattr(pdd_context, "PddOrderContextLookup", lookup_cls)
    monkeypatch.setattr(agent_module, "BridgeAgent", FakeAgent)
    run_pdd_client.install_local_first()
    return agent_module.BridgeAgent({
        "agent_id": "pdd-test",
        "agent_token": "token",
        "dual_write_local_workbench": False,
        "local_queue_path": str(tmp_path / "context.jsonl"),
        **cfg,
    })


def _user_event(index: int) -> dict:
    return {
        "platform": "pdd",
        "account": "cs_517301277:186558493",
        "buyer_id": f"buyer-{index}",
        "msg_id": f"ctx-{index}",
        "role": "user",
        "content": "hello",
        "ts": time.time(),
    }


def test_context_deadline_giveup_is_counted(monkeypatch, tmp_path) -> None:
    """到点放弃的查询必须计入 failed，否则现场看到的丢失率是低估的。"""
    calls: list[str] = []

    class SlowFirstLookup:
        def __init__(self, **_kwargs) -> None:
            pass

        def lookup(self, message):
            calls.append(str(message.get("buyer_id")))
            time.sleep(0.5)
            return normalize_order_response("517301277", {
                "ok": True, "total": 1, "orders": [_order("visible-order", "517301277")],
            })

    agent = _context_agent(monkeypatch, tmp_path, SlowFirstLookup,
                           pdd_context_workers=1,
                           pdd_context_timeout_seconds=0.4,
                           pdd_context_gate_seconds=0.4)
    try:
        agent._on_local_event(_user_event(0))
        deadline = time.time() + 3
        while time.time() < deadline and calls != ["buyer-0"]:
            time.sleep(0.02)
        agent._on_local_event(_user_event(1))
        deadline = time.time() + 3
        while time.time() < deadline and agent._pdd_context_stats["failed"] < 2:
            time.sleep(0.02)
        assert calls == ["buyer-0"]              # 第二条到点后直接放弃，不再占用查询
        assert agent._pdd_context_stats["failed"] == 2
        assert agent._pdd_context_stats["completed"] == 0
        assert agent._pdd_context_stats["last_error"] == "context_deadline_exceeded"
    finally:
        agent._stop.set()


def test_workbench_shop_refresh_signature_and_forced_redraw() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "s.shop_id || '', s.shop_name || ''" in html
    assert "if (shopId && shopName) shopNameMap[shopId] = shopName;" in html
    assert "shopNameMapChanged || !document.querySelector('.sess:hover')" in html


def test_order_context_workers_run_lookups_concurrently(monkeypatch, tmp_path) -> None:
    """1 分钟上百条时订单查询不能再是单线程串行，否则对应事件一直被挡在上传之外。"""
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    class FakeLookup:
        def __init__(self, **_kwargs) -> None:
            pass

        def lookup(self, _message):
            with guard:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            time.sleep(0.25)
            with guard:
                live["now"] -= 1
            return pdd_context._failure("fake")

    class FakeAgent(RealBridgeAgent):
        _local_first_v0512 = False
        _on_local_event = BASE_INGEST

        def __init__(self, cfg=None):
            self.cfg = dict(cfg or {})
            self.platform = type("Platform", (), {"name": "pdd"})()
            self._stop = threading.Event()
            self._pending = deque()
            self._pending_lock = threading.Lock()
            self._local_ingest_queue = None
            self._accounts_seen = {}
            self._last_error = ""
            self.queue_path = tmp_path / "pool.jsonl"

        def _flush_events(self, *, blocked_ids=None):
            return None

        def _status_payload(self):
            return {}

    monkeypatch.setattr(pdd_context, "PddOrderContextLookup", FakeLookup)
    monkeypatch.setattr(agent_module, "BridgeAgent", FakeAgent)
    run_pdd_client.install_local_first()
    agent = agent_module.BridgeAgent({
        "agent_id": "pdd-test",
        "agent_token": "token",
        "pdd_context_workers": 3,
        "dual_write_local_workbench": False,
        "local_queue_path": str(tmp_path / "pool.jsonl"),
    })
    try:
        for index in range(3):
            agent._on_local_event({
                "platform": "pdd",
                "account": "cs_517301277:186558493",
                "buyer_id": f"buyer-{index}",
                "msg_id": f"pool-{index}",
                "role": "user",
                "content": "hello",
                "ts": time.time(),
            })
        deadline = time.time() + 3
        while time.time() < deadline and live["peak"] < 2:
            time.sleep(0.02)
        assert live["peak"] >= 2, f"订单上下文查询没有并行（peak={live['peak']}）"
    finally:
        agent._stop.set()


def test_order_lookup_does_not_hold_lock_across_network(monkeypatch) -> None:
    """lookup() 的锁只保护目标缓存；Runtime.evaluate 的往返必须能并行。"""
    lookup = PddOrderContextLookup(timeout=1.0)
    monkeypatch.setattr(lookup, "_right_panel_targets", lambda _deadline: ["ws://fake-panel"])
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def fake_evaluate(_target, _expression, _deadline):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.2)
        with guard:
            live["now"] -= 1
        return {"ok": True, "total": 1, "orders": [_order("visible-order", "517301277")]}

    monkeypatch.setattr(lookup, "_evaluate", fake_evaluate)
    message = {"account": "cs_517301277:186558493", "buyer_id": "buyer-1"}
    threads = [threading.Thread(target=lookup.lookup, args=(message,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert live["peak"] >= 2, f"lookup 在锁内做了网络往返（peak={live['peak']}）"
