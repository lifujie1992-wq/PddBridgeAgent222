"""PDD 直发通道：通过注入 DLL 调用工作台内部 CMChatImpl::SendTextMsg。

与 CDP 模拟输入的区别：
- 不走 UI，不模拟打字，发送耗时 ~ 工作台自身网络往返
- 同步拿到 bool 返回值（CDP 路径拿不到发送结果，只能靠出站帧对账）

未注入 / 未就绪时所有调用返回 None，上层自动回退到原 CDP 路径，行为不变。
对应 DLL 工程见 D:\\temp\\pdd-send-hook
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

DEFAULT_PIPE = r"\\.\pipe\pdd_send_bridge"
_CONNECT_TIMEOUT_MS = 300
_REPLY_TIMEOUT_MS = 3000


class DirectSender:
    """命名管道客户端。单命令单连接（管道服务端是串行 accept）。"""

    def __init__(self, pipe: str = DEFAULT_PIPE, enabled: bool = True):
        self.pipe = pipe or DEFAULT_PIPE
        self.enabled = bool(enabled)
        self.ok_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self.last_error = ""

    def _exchange(self, line: str) -> str | None:
        """原始收发：连管道 -> 写一行 -> 读回复。测试会替换此方法。"""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 64 位句柄必须显式声明 restype，否则被截断（WriteFile 报 err=6）
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = k32.CreateFileW(self.pipe, 0xC0000000, 0, None, 3, 0, None)
        if handle in (None, wintypes.HANDLE(-1).value, -1):
            self.last_error = "pipe_unavailable(%d)" % ctypes.get_last_error()
            return None
        try:
            data = line.encode("utf-8")
            written = wintypes.DWORD(0)
            if not k32.WriteFile(wintypes.HANDLE(handle), data, len(data),
                                 ctypes.byref(written), None):
                self.last_error = "write_failed(%d)" % ctypes.get_last_error()
                return None
            buf = ctypes.create_string_buffer(4096)
            read = wintypes.DWORD(0)
            if not k32.ReadFile(wintypes.HANDLE(handle), buf, len(buf),
                                ctypes.byref(read), None) or not read.value:
                self.last_error = "read_failed(%d)" % ctypes.get_last_error()
                return None
            return buf.raw[: read.value].decode("utf-8", "replace").strip()
        finally:
            k32.CloseHandle(wintypes.HANDLE(handle))

    def ready(self) -> bool:
        if not self.enabled:
            return False
        reply = self._exchange("STATE")
        return bool(reply and reply.startswith("READY 1"))

    def send(self, uid: str, content: str, csid: str | None = None, account: str | None = None):
        """成功返回与 CDP 路径同构的 result dict；不可用返回 None（上层回退）。"""
        if not self.enabled:
            return None
        if not uid or not content:
            return None
        cmd = "TEXT|%s|%s|%s" % (csid or "", uid, content.replace("\r", " ").replace("\n", " "))
        started = time.monotonic()
        try:
            reply = self._exchange(cmd)
        except Exception as exc:  # 管道异常绝不影响主发送链路
            self.last_error = "exception:%s" % exc
            reply = None
        cost_ms = int((time.monotonic() - started) * 1000)
        if reply is None:
            self.skip_count += 1
            log.debug("直发通道不可用, 回退 CDP: %s", self.last_error)
            return None
        ok = reply.strip() == "OK 1"
        if ok:
            self.ok_count += 1
        else:
            self.fail_count += 1
        return {
            "ok": ok,
            "status": "sent" if ok else "failed",
            "real_send": ok,
            "retryable": not ok,
            "via": "dll_direct",
            "cost_ms": cost_ms,
            "buyer_id": uid,
            "csid": csid,
            "account": account,
            "reply": reply,
        }

    def stats(self) -> dict:
        return {"enabled": self.enabled, "ok": self.ok_count, "fail": self.fail_count,
                "skip": self.skip_count, "last_error": self.last_error, "pipe": self.pipe}
