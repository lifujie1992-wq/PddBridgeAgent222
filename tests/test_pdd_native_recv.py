"""原生接收通道（v0.7）单测：帧行解析 + feed_native_frame 复用帧管线 + 配置键。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from bridge.config import load_config
from bridge.pdd_recv import NativeReceiver, parse_recv_line
from bridge.pddbridge_source import PddbridgeSource

PUSH = ('{"response":"push","message":{"from":{"role":"user",'
        '"uid":"4764375385604","csid":"cs_12345:678"},'
        '"to":{"role":"mall_cs"},"msg_id":"m1","content":"你好",'
        '"type":0,"ts":1756000000000}}')


def test_parse_recv_line():
    assert parse_recv_line(b'FRAME|' + PUSH.encode()) == PUSH
    assert parse_recv_line('FRAME|' + PUSH) == PUSH
    assert parse_recv_line(b'') is None
    assert parse_recv_line(b'heartbeat') is None
    assert parse_recv_line(b'FRAME|') is None
    assert parse_recv_line(b'FRAME|\r') is None


def test_feed_native_frame_reuses_frame_pipeline():
    """原生帧必须走与 CDP 完全相同的归一化/去重/上报管线，不允许出现第二套逻辑。"""
    rows: list = []
    src = PddbridgeSource({}, on_event=rows.append)
    src.feed_native_frame(PUSH)
    assert src.msg_count == 1
    assert rows and rows[0]["buyer_id"] == "4764375385604"
    assert rows[0]["account"] == "cs_12345:678"


def test_native_config_defaults_are_off():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bridge_config.json"
        path.write_text("{}", encoding="utf-8")
        cfg = load_config(path)
    assert cfg["recv_via_dll"] is False
    assert cfg["recv_slot"] == -1
    assert cfg["recv_mode"] == 0


def test_receiver_configure_requires_confirmed_slot():
    """槽位未确认（-1）时绝不能下发 RECV_ON —— 真实虚表槽位必须先探针确认。"""
    recv = NativeReceiver(lambda raw: None, cfg={"recv_slot": -1})
    assert recv.configure() == "slot_not_configured"


def test_receiver_configure_sends_commands(monkeypatch):
    calls: list = []
    monkeypatch.setattr(NativeReceiver, "_command",
                        staticmethod(lambda text, timeout=3.0: calls.append(text) or "OK 1"))
    recv = NativeReceiver(lambda raw: None, cfg={"recv_slot": 5, "recv_mode": 1})
    assert recv.configure() == "OK 1"
    assert calls == ["RECV_SLOT|5", "RECV_MODE|1", "RECV_ON"]


def test_receiver_pump_parses_stream_and_survives_bad_frame(monkeypatch):
    """管道流式读循环：FRAME 混杂垃圾行、分包到达时帧仍逐条回调，不重复不丢。"""
    frames: list = []
    recv = NativeReceiver(frames.append, cfg={})

    class FakePipe:
        def __init__(self, payloads):
            self._chunks = list(payloads)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return self._chunks.pop(0) if self._chunks else b""

    payloads = [
        b"FRAME|" + PUSH.encode()[:20],          # 半包
        PUSH.encode()[20:] + b"\ngarbage\n",     # 续包 + 垃圾行
        b"FRAME|" + PUSH.encode() + b"\n",       # 完整第二帧
        b"",
    ]

    import bridge.pdd_recv as module

    def fake_open(path, *args, **kwargs):
        assert "pdd_recv_bridge" in str(path)
        return FakePipe(payloads)

    monkeypatch.setattr(module, "open", fake_open, raising=False)
    recv._pump()
    assert len(frames) == 2 and all(f == PUSH for f in frames)
