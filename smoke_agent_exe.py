from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_EXE = ROOT / "build-dist-agent" / "PddBridgeAgent" / "PddBridgeAgent.exe"


def main() -> int:
    exe = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_EXE
    result = subprocess.run(
        [str(exe), "--status"],
        cwd=str(exe.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    print("exit", result.returncode)
    print("stdout:\n" + result.stdout)
    print("stderr:\n" + result.stderr)
    if result.returncode != 0:
        return result.returncode
    required = ('"platform": "pdd"', '"agent_token_set": true')
    if any(value not in result.stdout for value in required):
        raise RuntimeError("agent status output is incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
