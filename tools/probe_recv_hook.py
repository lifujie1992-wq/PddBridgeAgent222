# -*- coding: utf-8 -*-
r"""临时验证：对已注入的 PddWorkbench 下发接收命令，并从管道读帧回 ACK。

用法：
    py probe_recv_hook.py hook          # 打开 msgsdk opt-in + 安装导出 detour
    py probe_recv_hook.py state
    py probe_recv_hook.py read 30       # 非阻塞读 30 秒
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

SEND = r"\\.\pipe\pdd_send_bridge_v4"
RECV = r"\\.\pipe\pdd_recv_bridge_v4"
ACK = r"\\.\pipe\pdd_recv_ack_v4"

INVALID = (None, -1, wintypes.HANDLE(-1).value)


def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    return k32


def command(text: str) -> str:
    k32 = _k32()
    handle = k32.CreateFileW(SEND, 0xC0000000, 0, None, 3, 0, None)
    if handle in INVALID:
        return "open_failed(%d)" % ctypes.get_last_error()
    try:
        data = text.encode()
        written = wintypes.DWORD(0)
        k32.WriteFile(wintypes.HANDLE(handle), data, len(data), ctypes.byref(written), None)
        buf = ctypes.create_string_buffer(4096)
        got = wintypes.DWORD(0)
        k32.ReadFile(wintypes.HANDLE(handle), buf, 4095, ctypes.byref(got), None)
        return buf.raw[: got.value].decode("utf-8", "replace").strip()
    finally:
        k32.CloseHandle(wintypes.HANDLE(handle))


def read_frames(seconds: float) -> None:
    k32 = _k32()
    recv = k32.CreateFileW(RECV, 0x80000000, 0, None, 3, 0, None)
    if recv in INVALID:
        print("接收管道打不开: err=%d" % ctypes.get_last_error())
        return
    ack = k32.CreateFileW(ACK, 0x40000000, 0, None, 3, 0, None)
    end = time.time() + seconds
    buf = b""
    count = 0
    while time.time() < end:
        avail, left = wintypes.DWORD(0), wintypes.DWORD(0)
        ok = k32.PeekNamedPipe(wintypes.HANDLE(recv), None, 0, None,
                               ctypes.byref(avail), ctypes.byref(left), None)
        if not ok:
            print("管道断开: err=%d" % ctypes.get_last_error())
            break
        if avail.value:
            blob = ctypes.create_string_buffer(avail.value)
            got = wintypes.DWORD(0)
            if k32.ReadFile(wintypes.HANDLE(recv), blob, avail.value,
                            ctypes.byref(got), None) and got.value:
                buf += blob.raw[: got.value]
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    if not text.startswith("FRAME|"):
                        continue
                    body = text[len("FRAME|"):]
                    seq_str, sep, payload = body.partition("|")
                    seq = int(seq_str) if sep and seq_str.isdigit() else 0
                    count += 1
                    print("[帧 %d] seq=%s len=%d" % (count, seq, len(payload)))
                    print("   " + payload[:300])
                    if ack not in INVALID:
                        data = ("ACK|%d\n" % seq).encode()
                        w = wintypes.DWORD(0)
                        k32.WriteFile(wintypes.HANDLE(ack), data, len(data),
                                      ctypes.byref(w), None)
        time.sleep(0.2)
    for handle in (recv, ack):
        if handle not in INVALID:
            k32.CloseHandle(wintypes.HANDLE(handle))
    print("共收到 %d 帧" % count)
    if not count:
        print("没有帧：detour 装上了，但这段时间没有推送，或者没有命中")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "hook"
    if action == "hook":
        print("RECV_MSGSdk_ON|1 ->", command("RECV_MSGSdk_ON|1"))
        print("RECV_HOOK        ->", command("RECV_HOOK"))
    print("STATE            ->", command("STATE"))
    print("RECV_STATE       ->", command("RECV_STATE"))
    if action == "read":
        read_frames(float(sys.argv[2]) if len(sys.argv) > 2 else 30.0)
