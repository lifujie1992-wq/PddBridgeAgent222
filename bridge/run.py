# -*- coding: utf-8 -*-
"""Absolute entrypoint for PyInstaller and `python bridge/run.py`.

Default: customer-friendly GUI.
  python bridge/run.py              # 拼多多
  python bridge/run.py --cli --status
"""
from __future__ import annotations

import sys


def _extract_platform(args: list[str]) -> tuple[str, list[str]]:
    plat = ""
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--platform="):
            plat = arg.split("=", 1)[1].strip()
        elif arg == "--platform" and i + 1 < len(args):
            plat = args[i + 1].strip()
            i += 1
        else:
            cleaned.append(arg)
        i += 1
    # Only 拼多多 is supported; anything else falls back to it.
    if plat != "pdd":
        plat = "pdd"
    return plat, cleaned


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plat, cleaned = _extract_platform(args)
    if "--cli" in cleaned or "--status" in cleaned or "--init-config" in cleaned:
        cleaned = [a for a in cleaned if a != "--cli"]
        # ensure platform propagates
        cleaned = ["--platform", plat, *cleaned]
        from bridge.agent import main as cli_main
        return cli_main(cleaned)
    from bridge.gui import main as gui_main
    return gui_main(plat)


if __name__ == "__main__":
    raise SystemExit(main())
