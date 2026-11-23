# -*- coding: utf-8 -*-
"""用真实的原生接收 spool 回放，验证 DLL 帧能否走完桥接管线。

用法（在仓库根目录）：
    set PYTHONPATH=.
    python tools/replay_native_spool.py D:\\temp\\pdd-send-hook\\out\\pdd_recv_spool.log

spool 行格式（DLL 侧持久化，用于断线重放）：
    F\t<seq>\t<len>\t<json>   收到的帧（<len> 是 JSON 字节数）
    A\t<seq>                 已 ACK

验收点：
  * 每一帧都被拆成 N 条买家消息，没有一条落进 unparsable / unknown_kind；
  * account 能由 target_id + mall_cs 端点推出（cs_<mall>:<target>）；
  * 平台 ts 原样保留，不被改写成当前时间。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bridge.pddbridge.protocol import native_push_messages, parse_frame
from bridge.pddbridge_source import PddbridgeSource


def read_spool(path: Path):
    with path.open("rb") as handle:
        for raw in handle:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("F\t"):
                continue
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            yield int(parts[1]), parts[3]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print("找不到 spool 文件: %s" % path)
        return 2

    events: list = []
    src = PddbridgeSource({}, on_event=events.append)
    kinds: dict[str, int] = {}
    frames = messages = 0

    for seq, raw in read_spool(path):
        frames += 1
        for frame in native_push_messages(raw):
            kind = str(parse_frame(frame).get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind != "push":
                continue
            before = len(events)
            src.feed_native_frame(frame)
            messages += len(events) - before

    print("spool 帧数        : %d" % frames)
    print("拆出的 push 条数  : %s" % json.dumps(kinds, ensure_ascii=False))
    print("产出买家消息      : %d" % messages)
    print("msg_count(内部)   : %d" % src.msg_count)
    print("丢弃计数          : %s" % json.dumps(
        {k: v for k, v in src.drops.items() if v}, ensure_ascii=False))
    for row in events[:5]:
        print("  - %s buyer=%s account=%s ts=%s %r" % (
            row.get("msg_id"), row.get("buyer_id"), row.get("account"),
            row.get("ts"), str(row.get("content"))[:24]))
    bad = sum(v for k, v in kinds.items() if k not in ("push",))
    return 1 if (bad or not messages) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
