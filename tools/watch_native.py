# -*- coding: utf-8 -*-
"""后台监控原生接收计数，变化就写一行到文件（供随时查看）。

用法：
    py watch_native.py 900        # 监控 900 秒
输出：C:\\Users\\...\\Temp\\native_watch.log
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Administrator\Documents\Codex\2026-09-29\x20\work\github-pdd\tools")
from probe_recv_hook import command  # noqa: E402

LOG = Path(r"C:\Users\Administrator\AppData\Local\Temp\native_watch.log")
KEEP = ("reg", "total", "q", "spool", "drop", "extfail", "notpush", "hits", "acked")


def snap() -> str:
    raw = command("RECV_STATE")
    data = dict(item.split("=", 1) for item in raw.split() if "=" in item)
    return " ".join("%s=%s" % (k, data.get(k)) for k in KEEP)


def main(seconds: int) -> None:
    end = time.time() + seconds
    last = None
    LOG.write_text("# native watch start %s\n" % time.strftime("%H:%M:%S"), encoding="utf-8")
    while time.time() < end:
        try:
            line = snap()
        except Exception as exc:  # 管道断了也要留痕
            line = "ERR %s" % exc
        if line != last:
            stamp = time.strftime("%H:%M:%S")
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write("%s  %s\n" % (stamp, line))
            last = line
        time.sleep(5)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write("# watch end %s\n" % time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 600)
