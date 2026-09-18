# -*- coding: utf-8 -*-
from pathlib import Path

import pytest

from bridge.config import load_config
from bridge.gui import BridgeGuiApp, ConfigDialog, _normalize_http_url


def test_normalize_center_url() -> None:
    assert _normalize_http_url(" http://10.0.0.5:18765/ ", "中心") == "http://10.0.0.5:18765"


def test_default_center_url(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.json", platform="pdd")["server_url"] == "http://47.107.138.228:18765"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18766",
        "http://localhost:18766/",
        "http://[::1]:18766",
    ],
)
def test_local_workbench_accepts_loopback(url: str) -> None:
    assert _normalize_http_url(url, "本地工作台", loopback=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:18766",
        "http://user:password@127.0.0.1:18766",
        "127.0.0.1:18766",
        "ftp://127.0.0.1:18766",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_local_workbench_rejects_invalid_or_authenticated_url(url: str) -> None:
    with pytest.raises(ValueError):
        _normalize_http_url(url, "本地工作台", loopback=True)


def test_gui_keeps_interactive_config_and_workbench_entry_points() -> None:
    source = Path(__file__).with_name("bridge").joinpath("gui.py").read_text(encoding="utf-8")
    assert "class ConfigDialog" in source
    assert "ConfigDialog(self)" in source
    assert 'text="打开本地工作台"' in source
    assert ConfigDialog.__name__ == "ConfigDialog"


def test_gui_logging_does_not_drop_pipeline_file_handler(tmp_path) -> None:
    """GUI 启动只能替换界面日志框，不能把 logs/bridge-pipeline.log 的 handler 摘掉
    （否则运行期日志 event queued/upload 一条都不会落盘）。"""
    import logging
    import queue

    root = logging.getLogger()
    before = list(root.handlers)
    handler = logging.FileHandler(tmp_path / "bridge-pipeline.log", encoding="utf-8")
    root.addHandler(handler)
    try:
        BridgeGuiApp._setup_logging(type("Stub", (), {"log_q": queue.Queue()})())
        assert handler in root.handlers
        logging.getLogger("pdd.local_first").info("runtime line lands in file")
        handler.flush()
        assert "runtime line lands in file" in (tmp_path / "bridge-pipeline.log").read_text(
            encoding="utf-8"
        )
    finally:
        for item in list(root.handlers):
            if item not in before:
                root.removeHandler(item)
        handler.close()
