from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

from smoke_gateway_lifecycle import (
    WM_CLOSE,
    find_window,
    gateway_status,
    wait_gateway,
)


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    base_url = sys.argv[2].rstrip("/")
    before = gateway_status(base_url)
    if not before:
        raise RuntimeError("expected an existing gateway before formal adoption test")
    process = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    try:
        hwnd = find_window(process.pid)
        adopted = wait_gateway(base_url, running=True)
        if adopted.get("pid") != before.get("pid"):
            raise RuntimeError("formal agent did not adopt the existing gateway pid")
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        process.wait(timeout=15)
        wait_gateway(base_url, running=False)
        print(json.dumps({
            "ok": True,
            "adopted_gateway_pid": before.get("pid"),
            "agent_exit_code": process.returncode,
            "gateway_stopped": True,
        }, ensure_ascii=False))
        return 0
    finally:
        if process.poll() is None:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
