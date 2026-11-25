# -*- coding: utf-8 -*-
"""升级后仍在用旧 DLL：配置里的 pdd_dll_path 指着上一版就白丢每次发送的 2 秒。"""
import os

from bridge.pdd_direct_inject import resolve_tool


def _touch(path, mtime):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def test_older_configured_dll_defers_to_packaged(tmp_path):
    """现场：配置写 D:\temp\...\pdd_send_v5.dll（9 月），包里是 v6（11 月）。"""
    app = tmp_path / "app"
    app.mkdir()
    packaged = _touch(app / "pdd_send_v6.dll", 2_000_000)
    configured = _touch(tmp_path / "pdd_send_v5.dll", 1_000_000)
    assert resolve_tool(str(configured), "pdd_send_v6.dll", "fallback",
                        alternates=("pdd_send_v5.dll",), roots=[app]) == str(packaged)


def test_newer_configured_dll_is_respected(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    _touch(app / "pdd_send_v6.dll", 1_000_000)
    configured = _touch(tmp_path / "pdd_send_v5.dll", 9_000_000)
    assert resolve_tool(str(configured), "pdd_send_v6.dll", "fallback",
                        alternates=("pdd_send_v5.dll",), roots=[app]) == str(configured)


def test_custom_filename_is_never_overridden(tmp_path):
    """客户自定义路径（不叫 pdd_send_v*）保持原样，只提示不替换。"""
    app = tmp_path / "app"
    app.mkdir()
    _touch(app / "pdd_send_v6.dll", 2_000_000)
    custom = _touch(tmp_path / "my_company_send.dll", 1_000_000)
    assert resolve_tool(str(custom), "pdd_send_v6.dll", "fallback", roots=[app]) == str(custom)


def test_empty_config_uses_packaged(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    packaged = _touch(app / "pdd_send_v6.dll", 2_000_000)
    assert resolve_tool("", "pdd_send_v6.dll", "fallback", roots=[app]) == str(packaged)
