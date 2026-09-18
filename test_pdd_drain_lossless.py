"""无损取帧（peek/ack）回归测试。

背景：旧 drain 是「先清空缓冲再靠 CDP 传输」，CDP 超时/丢包时那批消息永久消失 ——
进线量大时正好命中，表现就是「消息一多就丢」。这里守住三条：
  1) peek 模式下，帧处理完成后才 ack（页面才删）
  2) CDP 传输失败时不 ack（帧仍留在页面，下轮重取，不丢）
  3) 老注入脚本（无 peek）保持旧行为，不发 ack
"""
import pytest

from bridge.pddbridge_source import PddbridgeSource


class FakeCdp:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def eval(self, expression, context_id=None, **kw):
        self.calls.append(expression)
        if not self.results:
            return None
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        pass


class FakeSession:
    def __init__(self, cdp, cid=7):
        self.cdp = cdp
        self.cid = cid
        self.port = 57166
        self.frames = 0
        self.account = "mall-a"
        self.push_ok = True

    def label(self):
        return "fake"


def _source(stub_frames=True):
    src = PddbridgeSource(cfg={})
    if stub_frames:
        src._handle_frame = lambda e, s=None: None   # 隔离帧处理
    return src


def test_peek_mode_returns_lossless_and_frames():
    cdp = FakeCdp([{"hooked": 1, "lossless": True, "frames": [{"i": 1}, {"i": 2}], "stats": {}}])
    src = _source()
    frames, hooked, lossless = src._drain(FakeSession(cdp))
    assert frames == [{"i": 1}, {"i": 2}]
    assert hooked is True and lossless is True
    assert "__pddBridge_peek" in cdp.calls[0]      # 用的是无损接口


def test_pump_acks_after_processing_in_peek_mode():
    cdp = FakeCdp([{"hooked": 1, "lossless": True,
                    "frames": [{"i": 1}, {"i": 2}, {"i": 3}], "stats": {}}, 0])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "ok"
    assert len(cdp.calls) == 2
    assert "peek" in cdp.calls[0]
    assert "__pddBridge_ack(3)" in cdp.calls[1]    # 处理完才 ack，且数量一致


def test_transport_failure_does_not_ack_so_frames_survive():
    """CDP 超时（旧实现此刻已经丢数据）→ 不能 ack，页面缓冲必须原样保留。"""
    cdp = FakeCdp([RuntimeError("cdp timeout")])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "error"
    assert len(cdp.calls) == 1                      # 只发了 peek，没发 ack


def test_old_injection_without_peek_keeps_old_behavior():
    cdp = FakeCdp([{"hooked": 1, "lossless": False, "frames": [{"i": 1}], "stats": {}}])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "ok"
    assert len(cdp.calls) == 1                      # 老脚本自行清空，不发 ack


def test_hook_lost_reports_without_ack():
    cdp = FakeCdp([{"hooked": 0, "lossless": True, "frames": [], "stats": {}}])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "lost"
    assert len(cdp.calls) == 1
    assert src.drops["hook_lost"] >= 1


def test_hooked_without_attached_socket_is_treated_as_lost():
    cdp = FakeCdp([{"hooked": 1, "attached": 0, "lossless": True,
                    "frames": [{"i": 1}], "stats": {}}])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "lost"
    assert len(cdp.calls) == 1
    assert src.drops["hook_lost"] >= 1


def test_frame_error_is_not_acked():
    cdp = FakeCdp([{"hooked": 1, "attached": 1, "lossless": True,
                    "frames": [{"i": 1}], "stats": {}}])
    src = _source()
    src._handle_frame = lambda e, s=None: (_ for _ in ()).throw(RuntimeError("bad frame"))
    assert src._pump_session(FakeSession(cdp)) == "ok"
    assert len(cdp.calls) == 1
    assert src.drops["frame_error"] == 1


def test_ack_failure_is_swallowed():
    """ack 自身失败不能影响主流程（下轮会重取同一批，代价是重复而非丢失）。"""
    cdp = FakeCdp([{"hooked": 1, "lossless": True, "frames": [{"i": 1}], "stats": {}},
                   RuntimeError("ack boom")])
    src = _source()
    assert src._pump_session(FakeSession(cdp)) == "ok"
    assert len(cdp.calls) == 2


# ---------------- 历史快拉（pull_history）----------------

def test_pull_history_evals_helper_and_maps_result():
    from bridge.pddbridge_source import _SendWaiter
    cdp = FakeCdp([{"ok": True, "size": 100}])
    src = _source()
    src._session_for_send = lambda account: FakeSession(cdp)
    ev = _SendWaiter()
    src._do_pull_history("4764375385604", "mall-a", 100, "0", 0, "0", ev)
    assert ev.result["ok"] is True
    assert ev.result["buyer_id"] == "4764375385604"
    assert ev.result["via"] == "cdp_history"
    assert "__pddBridge_pullHistory" in cdp.calls[0]
    assert "100" in cdp.calls[0]


def test_pull_history_blocked_without_session():
    from bridge.pddbridge_source import _SendWaiter
    cdp = FakeCdp([])
    src = _source()
    src._session_for_send = lambda account: None
    ev = _SendWaiter()
    src._do_pull_history("1", "", 100, "0", 0, "0", ev)
    assert ev.result["ok"] is False and ev.result["status"] == "blocked"
    assert cdp.calls == []                      # 选不到会话就不该碰页面


def test_pull_history_survives_page_without_helper():
    from bridge.pddbridge_source import _SendWaiter
    cdp = FakeCdp([{"ok": False, "err": "no helper"}])
    src = _source()
    src._session_for_send = lambda account: FakeSession(cdp)
    ev = _SendWaiter()
    src._do_pull_history("1", "", 100, "0", 0, "0", ev)
    assert ev.result["ok"] is False
    assert ev.result["via"] == "cdp_history"    # 老注入也能优雅降级


# ---------------- localStorage 兜底重放（replayed -> 历史帧上报）----------------

def _push_frame(msg_id, content, ts):
    import json as _json
    return _json.dumps({
        "cmd": "push",
        "message": {"from": {"role": "user", "uid": "4764375385604"},
                    "to": {"role": "mall_cs"},
                    "msg_id": msg_id, "content": content,
                    "ts": str(int(ts))},
    }, ensure_ascii=False)


def _capture(src):
    captured = []
    src.report_all = True
    src.on_event = lambda msg: captured.append(msg)
    return captured


def test_replayed_frame_is_reported_as_history():
    """兜底重放的帧：必须按历史帧上报（is_history），中心不会拿旧消息去回复买家。"""
    import time
    src = _source(stub_frames=False)
    captured = _capture(src)
    src._handle_frame({"dir": "in", "t": 5, "replayed": 1,
                       "data": _push_frame("1789677000001", "溢出重放的消息", time.time())})
    assert captured, "重放帧应上报（中心没见过它，不报才是真丢）"
    assert captured[0].get("is_history") is True
    assert captured[0].get("capture_source") == "pdd_cdp_history"
    assert captured[0].get("auto_reply_eligible") is False


def test_live_frame_still_reported_as_live():
    """普通实时帧不受影响：不带 replayed 标记的仍按实时上报。"""
    import time
    src = _source(stub_frames=False)
    captured = _capture(src)
    src._handle_frame({"dir": "in", "t": 6,
                       "data": _push_frame("1789677000002", "实时消息", time.time())})
    assert captured
    assert not captured[0].get("is_history")
