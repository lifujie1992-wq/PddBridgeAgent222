# -*- coding: utf-8 -*-
"""1.0.12：桥接替代探域注入器，让便携包的 PddInsidePlugin 开出 0.7.6 跳转端口。"""
import asyncio
import configparser
import json
import os
import threading
import time

from bridge import inside_ipc
from bridge.inside_ipc import InsideIpcHost, handshake_body, plugin_dir_for


def _workbench(tmp_path, plugin_dir):
    exe_dir = tmp_path / "3.5.0.40_x64"
    exe_dir.mkdir()
    (exe_dir / "PDDData.ini").write_text(
        "[common]\nPddInsidePlugin=%s/PddInsidePlugin_x64.dll\n" % str(plugin_dir).replace("\\", "/"),
        encoding="utf-8")
    return str(exe_dir)


def test_handshake_section_is_workbench_pid():
    parser = configparser.ConfigParser()
    parser.read_string(handshake_body(26376, 39387, 4242, "3.5.0.40"))
    assert parser.sections() == ["26376"]
    row = parser["26376"]
    assert row["serverPort"] == "39387" and row["platformPid"] == "26376"
    assert row["targetProcessId"] == "4242" and row["platformVersion"] == "3.5.0.40"


def test_plugin_dir_from_pdddata_ini(tmp_path):
    plugin = tmp_path / "PddPortableRuntime" / "plugins"
    plugin.mkdir(parents=True)
    assert os.path.normcase(plugin_dir_for(_workbench(tmp_path, plugin))) == os.path.normcase(str(plugin))


def test_handshake_written_for_portable_but_not_for_tanyu(tmp_path, monkeypatch):
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    exe_dir = _workbench(tmp_path, plugin)
    host = InsideIpcHost({}, threading.Event())
    host.port = 40001
    monkeypatch.setattr(inside_ipc, "_workbenches", lambda: ([(777, exe_dir)], False))
    host._handshake_round()
    assert (plugin / "temp" / "InsideStartup_777.ini").is_file()

    (plugin / "temp" / "InsideStartup_777.ini").unlink()
    monkeypatch.setattr(inside_ipc, "_workbenches", lambda: ([(777, exe_dir)], True))   # 探域在跑
    host.handshakes.clear()
    host._handshake_round()
    assert not (plugin / "temp" / "InsideStartup_777.ini").exists()
    assert "探域" in host.status()["skipped"]["777"]


def test_plugin_connect_gets_init_then_start(monkeypatch):
    import websockets

    monkeypatch.setattr(inside_ipc, "_workbenches", lambda: ([], False))
    stop = threading.Event()
    host = InsideIpcHost({"platform_version": "3.5.0.40"}, stop)
    host.start()
    deadline = time.time() + 5
    while not host.port and time.time() < deadline:
        time.sleep(0.05)
    assert host.port

    async def plugin():
        url = "ws://127.0.0.1:%d/platformType=1&platformPid=555&insidePid=555&account=&identify=workbench" % host.port
        async with websockets.connect(url) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 3))
            second = json.loads(await asyncio.wait_for(ws.recv(), 3))
            await ws.send(json.dumps({"cmd": "handUp_status", "data": {}}))
            await asyncio.sleep(0.2)
            return first, second, list(host.connected)

    first, second, connected = asyncio.run(plugin())
    stop.set()
    assert first == {"cmd": "init", "data": {"logType": 4, "platformVersion": "3.5.0.40"}}
    assert second["cmd"] == "start_deal_msg"
    assert connected == [555]
    assert host.status()["msg_counts"] == {"handUp_status": 1}
