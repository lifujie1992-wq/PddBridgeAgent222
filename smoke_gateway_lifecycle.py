from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import psutil


user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


def gateway_status(base_url: str) -> dict:
    try:
        with urlrequest.urlopen(base_url + "/api/seat/status", timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) and payload.get("ok") else {}
    except (OSError, ValueError, urlerror.URLError):
        return {}


def wait_gateway(base_url: str, *, running: bool, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = gateway_status(base_url)
        if bool(last) is running:
            return last
        time.sleep(0.1)
    state = "running" if running else "stopped"
    raise RuntimeError(f"gateway did not become {state}: {last}")


def find_window(pid: int, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd: int, _lparam: int) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    raise RuntimeError(f"window not found for pid {pid}")


def start_agent(exe: Path) -> subprocess.Popen:
    return subprocess.Popen([str(exe)], cwd=str(exe.parent))


def close_agent_normally(process: subprocess.Popen) -> None:
    hwnd = find_window(process.pid)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    process.wait(timeout=15)


def kill_verified_gateway(bundle: Path, base_url: str) -> None:
    status = gateway_status(base_url)
    try:
        pid = int(status.get("pid") or 0)
        process = psutil.Process(pid)
        if Path(process.exe()).resolve() == (bundle / "LocalSeatGateway.exe").resolve():
            process.kill()
            process.wait(timeout=5)
    except (psutil.Error, OSError, TypeError, ValueError):
        pass


def main() -> int:
    bundle = Path(sys.argv[1]).resolve()
    exe = bundle / "PddBridgeAgent.exe"
    base_url = "http://127.0.0.1:18768"
    running_agents: list[subprocess.Popen] = []
    try:
        # Round 1: a gateway spawned by this agent must stop on normal close.
        first = start_agent(exe)
        running_agents.append(first)
        first_status = wait_gateway(base_url, running=True)
        close_agent_normally(first)
        running_agents.remove(first)
        wait_gateway(base_url, running=False)

        # Round 2: reproduce the reported orphan, then verify the new agent
        # adopts the exact bundle/config/port process and stops it on close.
        orphan_owner = start_agent(exe)
        running_agents.append(orphan_owner)
        orphan_status = wait_gateway(base_url, running=True)
        find_window(orphan_owner.pid)
        orphan_owner.kill()
        orphan_owner.wait(timeout=5)
        running_agents.remove(orphan_owner)
        surviving_status = wait_gateway(base_url, running=True)
        if surviving_status.get("pid") != orphan_status.get("pid"):
            raise RuntimeError("orphan gateway pid changed unexpectedly")

        adopter = start_agent(exe)
        running_agents.append(adopter)
        find_window(adopter.pid)
        adopted_status = wait_gateway(base_url, running=True)
        close_agent_normally(adopter)
        running_agents.remove(adopter)
        wait_gateway(base_url, running=False)

        print(json.dumps({
            "ok": True,
            "spawned_gateway_pid": first_status.get("pid"),
            "adopted_gateway_pid": adopted_status.get("pid"),
            "normal_close_stopped_gateway": True,
            "orphan_adoption_stopped_gateway": True,
        }, ensure_ascii=False))
        return 0
    finally:
        for process in running_agents:
            if process.poll() is None:
                process.kill()
        kill_verified_gateway(bundle, base_url)


if __name__ == "__main__":
    raise SystemExit(main())
