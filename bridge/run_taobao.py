# -*- coding: utf-8 -*-
"""Qianniu / Taobao bridge entry (separate EXE packaging)."""
from bridge.run import main

if __name__ == "__main__":
    raise SystemExit(main(["--platform", "taobao"]))
