"""原生接收通道（v0.7）单测：帧行解析 + feed_native_frame 复用帧管线 + 配置键。"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from bridge.config import load_config
from bridge.pdd_recv import NativeReceiver, parse_recv_line
from bridge.pddbridge_source import PddbridgeSource

PUSH = ('{"response":"push","message":{"from":{"role":"user",'
        '"uid":"3000000000001","csid":"cs_12345:678"},'
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
    assert rows and rows[0]["buyer_id"] == "3000000000001"
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


def test_identity_derivation_differs_by_mac(monkeypatch):
    """克隆盘复制了注册表 MachineGuid, 只有 MAC 能区分两台机器 ——
    身份派生必须包含 MAC, 否则两台桥接以同一身份注册中心互相顶号。"""
    import uuid
    from bridge import config as cfg_mod

    monkeypatch.setattr(cfg_mod.uuid, "getnode", lambda: 0xAAAAAAAAAAAA)
    d1 = cfg_mod.machine_device_id()
    monkeypatch.setattr(cfg_mod.uuid, "getnode", lambda: 0xBBBBBBBBBBBB)
    d2 = cfg_mod.machine_device_id()
    assert d1 != d2 and d1.startswith("device-")

    import socket as _socket
    monkeypatch.setattr(cfg_mod.uuid, "getnode", lambda: 0xAAAAAAAAAAAA)
    a1 = uuid.uuid5(uuid.NAMESPACE_DNS, _socket.gethostname() + "|x|" + "%012x" % 0xAAAAAAAAAAAA).hex[:12]
    monkeypatch.setattr(cfg_mod.uuid, "getnode", lambda: 0xBBBBBBBBBBBB)
    a2 = uuid.uuid5(uuid.NAMESPACE_DNS, _socket.gethostname() + "|x|" + "%012x" % 0xBBBBBBBBBBBB).hex[:12]
    assert a1 != a2


# ---------------- 0.7.7: 对齐 DLL v4 管道协议（seq + ACK）+ 原生 push 信封 ----------------
# 现场教训：DLL 发的是 FRAME|<seq>|<json> 并等 ACK|<seq>；老实现只切 FRAME|，
# 既解析失败又从不确认，spool 无限重放。下面四条守住这条链路。

WB_PUSH = ('{"push_type":2,"push_data":{"seq_type":1,"seq_id":977,"base_seq_id":976,'
           '"data":[{"message":{"from":{"role":"user","uid":"3000000000001"},'
           '"to":{"uid":"427302374","role":"mall_cs"},"content":"怎么操作",'
           '"client_msg_id":"79BD7014","type":0,"ts":"__TS__","version":1,'
           '"msg_id":"1789769315558","pre_msg_id":"1789769125670","nickname":"富**",'
           '"push_biz_context":{"msg_category":1}},"chat_type_id":1}]},'
           '"target_id":164945148,"custom_data":{"fold_mute_notice":1}}')
# 平台 ts 用当前秒：真实现场帧必然是近实时的，写死历史时间会被 stamp_message 判成 is_history
WB_PUSH = WB_PUSH.replace("__TS__", str(int(time.time())))


def test_parse_recv_line_ex_keeps_dll_sequence():
    """DLL v4: FRAME|<seq>|<json>；老 DLL 无 seq，必须仍然兼容。"""
    from bridge.pdd_recv import parse_recv_line_ex

    assert parse_recv_line_ex("FRAME|7|" + WB_PUSH) == (7, WB_PUSH)
    assert parse_recv_line_ex(b"FRAME|0|" + PUSH.encode()) == (0, PUSH)
    assert parse_recv_line_ex("FRAME|" + PUSH) == (0, PUSH)      # 老格式
    assert parse_recv_line_ex("FRAME|7|") is None
    assert parse_recv_line_ex("ACK|7") is None
    # 兼容接口仍然只返回 JSON
    assert parse_recv_line("FRAME|7|" + PUSH) == PUSH


def test_pipe_paths_match_dll_v4_and_suffix():
    """管道名必须和 DLL 一致：pdd_recv_bridge_v4<sfx> / pdd_recv_ack_v4<sfx>。"""
    from bridge.pdd_recv import ack_pipe_path, recv_pipe_path

    assert recv_pipe_path({}) == "\\\\.\\pipe\\pdd_recv_bridge_v4"
    assert ack_pipe_path({}) == "\\\\.\\pipe\\pdd_recv_ack_v4"
    assert recv_pipe_path({"recv_pipe_suffix": "seat01"}) == "\\\\.\\pipe\\pdd_recv_bridge_v4seat01"
    assert ack_pipe_path({"recv_pipe_suffix": "seat01"}) == "\\\\.\\pipe\\pdd_recv_ack_v4seat01"
    # 非法字符替换成 _（DLL 侧同样的净化规则），避免拼出非法管道名
    assert recv_pipe_path({"recv_pipe_suffix": "a b:c"}) == "\\\\.\\pipe\\pdd_recv_bridge_v4a_b_c"


def test_workbench_push_envelope_is_unwrapped_to_buyer_message():
    """push_data.data 是数组，一次推送可能多条；必须逐条拆成页面 push 帧。"""
    from bridge.pddbridge.protocol import native_push_messages

    frames = native_push_messages(WB_PUSH)
    assert len(frames) == 1
    assert frames[0]["cmd"] == "push"
    assert frames[0]["target_id"] == 164945148
    assert frames[0]["message"]["msg_id"] == "1789769315558"
    assert frames[0]["message"]["ts"] == WB_PUSH.split('"ts":"')[1].split('"')[0]

    two = ('{"push_type":2,"push_data":{"data":[{"message":{"msg_id":"m1","ts":"1","content":"a"}},'
           '{"message":{"msg_id":"m2","ts":"2","content":"b"}}]},"target_id":42}')
    assert [f["message"]["msg_id"] for f in native_push_messages(two)] == ["m1", "m2"]

    # 已经是页面帧形状：原样放行，不再套一层
    assert native_push_messages(PUSH)[0]["response"] == "push"
    assert native_push_messages("not json") == []
    assert native_push_messages({"push_type": 9}) == []


def test_feed_native_frame_end_to_end_from_workbench_envelope():
    """原生信封走完管线后必须得到买家消息，且是实时（非历史）事件。"""
    rows: list = []
    src = PddbridgeSource({}, on_event=rows.append)
    src.feed_native_frame(WB_PUSH)
    assert src.msg_count == 1
    assert rows and rows[0]["msg_id"] == "1789769315558"
    assert rows[0]["buyer_id"] == "3000000000001"
    assert rows[0]["account"] == "cs_427302374:164945148"
    assert rows[0]["content"] == "怎么操作"
    assert not rows[0].get("is_history")


def test_receiver_writes_ack_after_successful_callback(monkeypatch):
    """每帧处理成功才回 ACK|<seq>；回调抛异常的那帧不回（等 DLL 重发）。"""
    import bridge.pdd_recv as module

    acks: list = []
    got: list = []

    class FakePipe:
        def __init__(self, payloads):
            self._chunks = list(payloads)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return self._chunks.pop(0) if self._chunks else b""

        def write(self, data):
            acks.append(data)
            return len(data)

        def close(self):
            pass

    payloads = [b"FRAME|1|" + PUSH.encode() + b"\n",
                b"FRAME|2|" + PUSH.encode() + b"\n",
                b""]

    def fake_open(path, *args, **kwargs):
        if "pdd_recv_ack" in str(path):
            return FakePipe([])
        return FakePipe(payloads)

    def on_frame(raw):
        got.append(raw)
        if len(got) == 2:
            raise RuntimeError("下游炸了")

    monkeypatch.setattr(module, "open", fake_open, raising=False)
    recv = module.NativeReceiver(on_frame, cfg={})
    recv._pump()
    assert got == [PUSH, PUSH]
    assert acks == [b"ACK|1\n"]          # 第 2 帧抛异常 → 不回 ACK
    assert recv.acked == 1


# ---------------- 0.7.8: 90 秒门槛可配（客户 0.7.7.0 复测第 1 条） ----------------
def test_history_ready_window_is_configurable():
    """历史事件的上送门槛：0=立即、N=等 N 秒、None=旧行为 90 秒。"""
    from bridge.message_timing import LIVE_MAX_AGE_SECONDS, history_ready, hold_remaining

    now = 1_800_000_000.0
    fresh = {"is_history": True, "ts": now - 10}      # 10 秒前
    old = {"is_history": True, "ts": now - 120}       # 2 分钟前
    live = {"is_history": False, "ts": now - 10}

    assert LIVE_MAX_AGE_SECONDS == 90.0
    assert history_ready(fresh, now) is False          # 旧行为：90 秒内不发
    assert history_ready(old, now) is True
    assert history_ready(live, now) is True            # 实时消息不受门槛影响

    assert history_ready(fresh, now, 0) is True        # ack_capable：立即发
    assert history_ready(fresh, now, 5) is True        # 已过 10s > 5s：放行
    assert history_ready(fresh, now, 15) is False      # 门槛 15s：还差 5s
    assert hold_remaining(fresh, now, 30) == 20.0
    assert hold_remaining(fresh, now, 0) == 0.0
    # 没有平台时间的永远不发（否则会被当成当前时间回填）
    assert history_ready({"is_history": True, "ts": 0}, now, 0) is False


def test_history_hold_policy_maps_to_window():
    """policy 与显式窗口的优先级。

    load_config 总会把 history_live_window_seconds 补成 90，所以不能直接读它判断，
    必须看 `_explicit_keys`（用户是否真的写过这个键）——否则 ack_capable 永远无效。
    """
    from bridge.agent import BridgeAgent

    class Stub:
        pass

    cases = [
        # (cfg, 期望秒数)
        ({"history_hold_policy": "legacy"}, 90.0),
        ({}, 90.0),
        ({"history_hold_policy": "ack_capable"}, 0.0),          # 补出来的 90 不算显式
        ({"history_hold_policy": "off"}, 0.0),
        ({"history_hold_policy": "ack_capable", "history_live_window_seconds": 15,
          "_explicit_keys": ["history_live_window_seconds"]}, 15.0),
        ({"history_live_window_seconds": 0, "_explicit_keys": ["history_live_window_seconds"]}, 0.0),
        ({"history_hold_policy": "legacy", "history_live_window_seconds": 30,
          "_explicit_keys": ["history_live_window_seconds"]}, 30.0),
    ]
    for cfg, expected in cases:
        stub = Stub()
        stub.cfg = cfg
        assert BridgeAgent._history_window(stub) == expected, cfg


# ---------------- 0.7.11: 导出 detour 接收路径（不依赖虚表槽位） ----------------
def test_hook_install_opts_in_then_installs(monkeypatch):
    """RECV_HOOK 默认被 DLL 拒绝（unverified 路径），必须先 opt-in。

    这条路径按 MsgSdk 导出名（NotifyWSMessage / CheckWSMsg）定位，不需要探针，
    因此不会踩 3.5.0.40 上“未初始化临界区”崩溃的坑。
    """
    calls: list = []
    monkeypatch.setattr(NativeReceiver, "_command",
                        lambda self, text, timeout=3.0: calls.append(text) or "OK hook=0 notify=1 check=1")
    recv = NativeReceiver(lambda raw: None, cfg={"recv_slot": -1})
    assert recv.hook_install() == "OK hook=0 notify=1 check=1"
    assert calls == ["RECV_MSGSdk_ON|1", "RECV_HOOK"]


def test_probe_is_opt_in_not_default():
    """探针会崩客户端，配置默认必须是关的。"""
    from bridge.config import load_config
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bridge_config.json"
        path.write_text("{}", encoding="utf-8")
        cfg = load_config(path)
    assert cfg["recv_probe"] is False
    assert cfg["recv_slot"] == -1
