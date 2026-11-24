# -*- coding: utf-8 -*-
r"""注入 DLL 的命名管道名（单一事实来源）。

DLL v5 给三条管道都加了协议版本后缀和可选实例后缀：

    \\.\pipe\pdd_send_bridge_v4<suffix>     发送/命令
    \\.\pipe\pdd_recv_bridge_v4<suffix>     接收帧流
    \\.\pipe\pdd_recv_ack_v4<suffix>        接收 ACK

桥接这边以前写的是不带后缀的 ``pdd_send_bridge``，于是：
  * ``send_via_dll`` 直发永远连不上 → 静默回退 CDP；
  * ``RECV_SLOT`` / ``PROBE`` 命令下发失败 → 原生接收永远起不来。
两条都要按 DLL 的真实名字来，并保留老名字作为兜底（老 DLL 没有后缀）。
"""
from __future__ import annotations

import os
import re

PIPE_VERSION = "v4"
_SANITIZE = re.compile(r"[^0-9A-Za-z_-]")

SEND_PREFIX = "pdd_send_bridge"
RECV_PREFIX = "pdd_recv_bridge"
ACK_PREFIX = "pdd_recv_ack"

PIPE_ROOT = "\\\\.\\pipe\\"


def suffix(cfg: dict | None = None) -> str:
    """实例后缀。DLL 读工作台进程的 PDD_BRIDGE_PIPE_SUFFIX，桥接侧必须用同一个值。"""
    value = str((cfg or {}).get("recv_pipe_suffix")
                or os.environ.get("PDD_BRIDGE_PIPE_SUFFIX") or "")
    return _SANITIZE.sub("_", value)


def _build(prefix: str, cfg: dict | None, versioned: bool = True) -> str:
    tail = ("_" + PIPE_VERSION if versioned else "") + suffix(cfg)
    return PIPE_ROOT + prefix + tail


def send_pipe(cfg: dict | None = None) -> str:
    explicit = str((cfg or {}).get("send_via_dll_pipe") or "").strip()
    return explicit or _build(SEND_PREFIX, cfg)


def recv_pipe(cfg: dict | None = None) -> str:
    explicit = str((cfg or {}).get("recv_pipe_name") or "").strip()
    return explicit or _build(RECV_PREFIX, cfg)


def ack_pipe(cfg: dict | None = None) -> str:
    explicit = str((cfg or {}).get("recv_ack_pipe_name") or "").strip()
    return explicit or _build(ACK_PREFIX, cfg)


def legacy(prefix: str, cfg: dict | None = None) -> str:
    """老 DLL（无版本后缀）的管道名，作为兜底。"""
    return _build(prefix, cfg, versioned=False)


def send_candidates(cfg: dict | None = None) -> list[str]:
    """直发/命令下发的候选管道：新的带版本名优先，老名字兜底。"""
    explicit = str((cfg or {}).get("send_via_dll_pipe") or "").strip()
    if explicit:
        return [explicit]
    return [send_pipe(cfg), legacy(SEND_PREFIX, cfg)]
