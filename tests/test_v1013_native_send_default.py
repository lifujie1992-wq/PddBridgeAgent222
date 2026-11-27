# -*- coding: utf-8 -*-
"""1.0.13：插件由桥接接管时默认不走原生发送（现场「插件报成功、买家没收到」）。"""
import types

from bridge.platforms import pdd as pdd_platform


def _status(monkeypatch, cfg):
    monkeypatch.setattr(pdd_platform.pdd_channel, "channel_status",
                        lambda **_kw: {"dll_port": 62886, "dll_ready": True})
    monkeypatch.setattr("bridge.pddbridge_source.channel_status_cdp",
                        lambda _cfg: {"send_ready": True}, raising=False)
    plat = pdd_platform.PddPlatform() if hasattr(pdd_platform, "PddPlatform") else pdd_platform.PLATFORM
    base = {"tanyu_log_dir": "C:/x", "data_source": "tanyu_logs", "_pddbridge_source": object()}
    return plat.channel_status({**base, **cfg})["send_channel"]


def test_bridge_owned_plugin_defaults_to_page_send(monkeypatch):
    host = types.SimpleNamespace(connected={8384: 1.0})
    assert _status(monkeypatch, {"_inside_ipc": host}) == "cdp"


def test_tanyu_owned_plugin_keeps_native_send(monkeypatch):
    host = types.SimpleNamespace(connected={})          # 桥接没连上 = 端口是探域开的
    assert _status(monkeypatch, {"_inside_ipc": host}) == "tanyu"
    assert _status(monkeypatch, {}) == "tanyu"


def test_explicit_config_wins(monkeypatch):
    host = types.SimpleNamespace(connected={8384: 1.0})
    assert _status(monkeypatch, {"_inside_ipc": host, "inside_native_send": True}) == "tanyu"
    assert _status(monkeypatch, {"inside_native_send": False}) == "cdp"
