from __future__ import annotations

import json
import threading
from pathlib import Path

import run_pdd_client


def test_adsorb_autostart_defaults_enabled(tmp_path: Path) -> None:
    assert run_pdd_client._adsorb_auto_start_enabled(tmp_path) is True


def test_adsorb_autostart_can_be_disabled(tmp_path: Path) -> None:
    (tmp_path / "pdd_adsorb_config.json").write_text(
        json.dumps({"auto_start_with_bridge": False}),
        encoding="utf-8",
    )
    assert run_pdd_client._adsorb_auto_start_enabled(tmp_path) is False


def test_start_adsorb_uses_bundle_companion(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "PddAdsorbWindow.exe"
    executable.write_bytes(b"MZ")
    calls: list[tuple[list[str], dict]] = []

    class Process:
        pid = 4321

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(run_pdd_client, "_running_adsorb_pid", lambda *_: None)
    monkeypatch.setattr(run_pdd_client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_pdd_client, "_ADSORB_PROCESS_PID", None)

    assert run_pdd_client.start_adsorb_window(tmp_path) is True
    assert calls[0][0] == [str(executable)]
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert run_pdd_client._ADSORB_PROCESS_PID == 4321


def test_start_adsorb_does_not_duplicate_existing_instance(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "PddAdsorbWindow.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setattr(run_pdd_client, "_running_adsorb_pid", lambda *_: 7788)

    assert run_pdd_client.start_adsorb_window(tmp_path) is False


def test_first_run_retry_starts_dock_after_gateway(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(run_pdd_client, "_gateway_config_ready", lambda: True)
    monkeypatch.setattr(run_pdd_client, "start_local_gateway", lambda: calls.append("gateway"))
    monkeypatch.setattr(run_pdd_client, "start_adsorb_window", lambda: calls.append("adsorb"))

    run_pdd_client._retry_local_gateway_after_setup(threading.Event())

    assert calls == ["gateway", "adsorb"]


def test_first_run_retry_does_not_start_dock_after_shutdown(monkeypatch) -> None:
    calls: list[str] = []
    stop_event = threading.Event()
    monkeypatch.setattr(run_pdd_client, "_gateway_config_ready", lambda: True)

    def start_gateway() -> None:
        calls.append("gateway")
        stop_event.set()

    monkeypatch.setattr(run_pdd_client, "start_local_gateway", start_gateway)
    monkeypatch.setattr(run_pdd_client, "start_adsorb_window", lambda: calls.append("adsorb"))

    run_pdd_client._retry_local_gateway_after_setup(stop_event)

    assert calls == ["gateway"]
