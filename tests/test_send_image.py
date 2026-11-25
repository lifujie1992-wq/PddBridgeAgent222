# -*- coding: utf-8 -*-
"""发图片：命令解析 + 本地/URL 落地 + DLL 直发。

现场：客户话术库配了「文字 + 图片」，实际只发了文字 —— 桥接侧根本没有图片通道。
"""
from __future__ import annotations

import os
import tempfile

from bridge import media
from bridge.agent import BridgeAgent
from bridge.pdd_send_direct import DirectSender


def _png(tmp: str, name: str = "a.png") -> str:
    path = os.path.join(tmp, name)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return path


def test_command_images_reads_several_shapes():
    read = BridgeAgent._command_images          # staticmethod
    assert read({"images": ["http://a/1.png"]}) == ["http://a/1.png"]
    assert read({"meta": {"images": ["x.png", "y.png"]}}) == ["x.png", "y.png"]
    assert read({"image_url": "http://a/2.png"}) == ["http://a/2.png"]
    assert read({"type": "send_image", "content": "c.png"}) == ["c.png"]
    # 去重 + 顺序稳定
    assert read({"images": ["a.png", "a.png"], "image": "b.png"}) == ["a.png", "b.png"]
    assert read({}) == []


def test_resolve_local_image():
    with tempfile.TemporaryDirectory() as tmp:
        path = _png(tmp)
        resolved, error = media.resolve_image(path)
        assert resolved == path and error == ""
        missing, error = media.resolve_image(os.path.join(tmp, "nope.png"))
        assert missing is None and "not found" in error
        empty = os.path.join(tmp, "empty.png")
        open(empty, "wb").close()
        assert media.resolve_image(empty)[0] is None


def test_localize_many_splits_ok_and_failed():
    with tempfile.TemporaryDirectory() as tmp:
        good = _png(tmp)
        paths, errors = media.localize_many([good, os.path.join(tmp, "bad.png")])
        assert paths == [good] and len(errors) == 1


def test_direct_sender_image_uses_image_command(monkeypatch):
    sent = []

    def fake_exchange(self, line):
        sent.append(line)
        return "OK 1"

    monkeypatch.setattr(DirectSender, "_exchange", fake_exchange)
    sender = DirectSender("")
    result = sender.send_image("3000000000001", r"C:\tmp\a.png", "cs_1:100", "cs_1:100")
    assert result["ok"] is True
    assert result["via"] == "dll_direct_image"
    assert sent and sent[0].startswith("IMAGE|cs_1:100|3000000000001|")

    sent.clear()
    sender._exchange = lambda line: "OK 0"      # type: ignore[assignment]
    failed = sender.send_image("3000000000001", r"C:\tmp\a.png")
    assert failed["ok"] is False and failed["status"] == "failed"


def test_send_image_without_dll_reports_unsupported():
    from bridge.pddbridge_source import PddbridgeSource

    src = PddbridgeSource({}, on_event=lambda m: None)
    result = src.send_image("3000000000001", r"C:\tmp\a.png", "cs_1:100")
    assert result["ok"] is False
    assert result["error"] == "image_channel_unavailable"
    assert "发图片" in result["error_user"]
