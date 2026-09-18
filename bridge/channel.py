# -*- coding: utf-8 -*-
"""Local PddWorkbench DLL discovery and send_text (no AI, no store policy)."""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest


_PORT_CACHE: Dict[str, Any] = {
    "expires": 0.0,
    "dll_port": None,
    "pid": None,
    "state": "not_found",
    "process_map": {},
}


def _hidden_subprocess_kwargs() -> dict:
    """Prevent PowerShell/cmd black console flash on Windows GUI apps."""
    kwargs: dict = {}
    if sys.platform == "win32":
        # Python 3.7+
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = create_no_window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _pids_by_process_name(process_name: str) -> list[int]:
    """Resolve PIDs for a process name without PowerShell (fast path)."""
    name = str(process_name or "").strip()
    if not name:
        return []
    needles = {name.lower(), f"{name.lower()}.exe"}
    pids: list[int] = []
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name", "pid"]):
            n = (p.info.get("name") or "").lower()
            if n in needles or n.replace(".exe", "") == name.lower():
                try:
                    pids.append(int(p.info["pid"]))
                except Exception:
                    continue
        return pids
    except Exception:
        pass
    # fallback: tasklist (still faster/safer than Get-NetTCPConnection loops)
    try:
        raw = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            timeout=3,
            **_hidden_subprocess_kwargs(),
        )
        text = raw.decode("gbk", "replace")
        for line in text.splitlines():
            # "name.exe","1234","Session Name","Session#","Mem"
            parts = [x.strip().strip('"') for x in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
    except Exception:
        pass
    return pids


def _ps_process_listen_map(process_name: str) -> Dict[int, List[int]]:
    """Map pid -> listen ports via psutil/netstat only. Never PowerShell (can hang forever)."""
    pids = _pids_by_process_name(process_name)
    if not pids:
        return {}
    port_map = _netstat_listen_ports_for_pids(pids)
    if port_map:
        return port_map
    # Empty listen set is fine — caller treats as not_found. Do NOT call Get-NetTCPConnection.
    return {pid: [] for pid in pids}


def discover_ports(
    *,
    force: bool = False,
    configured_port: Any = None,
    configured_pid: Any = None,
) -> Tuple[Optional[int], Optional[int], str]:
    """Return (dll_port, workbench_pid, state)."""
    now = time.time()
    if not force and now < float(_PORT_CACHE.get("expires") or 0):
        return _PORT_CACHE.get("dll_port"), _PORT_CACHE.get("pid"), str(_PORT_CACHE.get("state") or "")

    process_map = _ps_process_listen_map("PddWorkbench")
    try:
        cfg_pid = int(configured_pid) if configured_pid not in (None, "") else None
    except (TypeError, ValueError):
        cfg_pid = None
    try:
        cfg_port = int(configured_port) if configured_port not in (None, "") else None
    except (TypeError, ValueError):
        cfg_port = None

    dll_port: Optional[int] = None
    pid: Optional[int] = None
    state = "not_found"
    if cfg_port:
        dll_port, pid, state = cfg_port, cfg_pid, "configured"
        if pid is None and len(process_map) == 1:
            pid = next(iter(process_map))
    elif cfg_pid is not None:
        ports = process_map.get(cfg_pid, [])
        pid = cfg_pid
        if len(ports) == 1:
            dll_port, state = ports[0], "single_port_for_configured_pid"
        elif len(ports) > 1:
            state = "ambiguous_ports"
    elif len(process_map) == 1:
        pid, ports = next(iter(process_map.items()))
        if len(ports) == 1:
            dll_port, state = ports[0], "single_process_single_port"
        elif len(ports) > 1:
            state = "ambiguous_ports"
    elif len(process_map) > 1:
        state = "ambiguous_processes"

    _PORT_CACHE.update(
        expires=now + 30.0,
        dll_port=dll_port,
        pid=pid,
        state=state,
        process_map=process_map,
    )
    return dll_port, pid, state


def _dll_response_error(http_status: int, raw_body: bytes) -> str:
    if not 200 <= int(http_status) < 300:
        return f"HTTP {http_status}"
    text = raw_body.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("msg") or payload.get("message") or payload.get("error") or "").strip()
    if payload.get("ok") is False or payload.get("success") is False:
        return message or "DLL rejected the request"
    code = payload.get("code")
    if code not in (None, "", 0, "0", 200, "200"):
        return message or f"DLL error code {code}"
    status = str(payload.get("status") or "").strip().lower()
    if status in {"error", "failed", "failure", "rejected"}:
        return message or f"DLL status {status}"
    return ""


_QN_PORT_CACHE: Dict[str, Any] = {
    "expires": 0.0,
    "dll_port": None,
    "pid": None,
    "state": "not_found",
    "process_map": {},
}


def _probe_qn_dll_http_port(port: int, *, timeout: float = 0.35) -> bool:
    """True if port answers 探域 QN httpTestApi (may be on AliRender, not AliWorkbench)."""
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False
    if not port_i:
        return False
    try:
        body = b'{"cmd":"httpTest","platformType":0,"data":{}}'
        req = urlrequest.Request(
            f"http://127.0.0.1:{port_i}/tanyu/client/qn/dll/httpTestApi",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not (200 <= int(resp.status) < 300):
                return False
            # any JSON-ish success is enough; 500 on wrong path already filtered
            text = raw.decode("utf-8", "replace")
            return "success" in text.lower() or text.startswith("{")
    except Exception:
        return False


def _http_discover_qn_dll_port() -> Tuple[Optional[int], Optional[int], str]:
    """Find QN DLL port by probing listen ports (AliWorkbench + AliRender).

    Real deployment often binds /tanyu/client/qn/dll on AliRender (e.g. 43800),
    not on AliWorkbench.exe — process-name map alone misses it.
    """
    candidates: list[tuple[int, Optional[int]]] = []  # (port, pid)
    seen: set[int] = set()
    for proc_name in ("AliWorkbench", "AliRender", "Injector_taobao"):
        pmap = _ps_process_listen_map(proc_name)
        for pid, ports in pmap.items():
            for port in ports:
                if port in seen:
                    continue
                seen.add(port)
                candidates.append((port, pid))
    # Prefer mid/high app ports first (skip very low system)
    candidates.sort(key=lambda x: (0 if 10000 <= x[0] <= 60000 else 1, -x[0]))
    for port, pid in candidates[:40]:
        if _probe_qn_dll_http_port(port):
            return port, pid, "http_probe"
    return None, None, "http_probe_miss"


def discover_qn_ports(
    *,
    force: bool = False,
    configured_port: Any = None,
    configured_pid: Any = None,
) -> Tuple[Optional[int], Optional[int], str]:
    """Discover 探域 QN DLL HTTP port (/tanyu/client/qn/dll/httpTestApi)."""
    now = time.time()
    if not force and now < float(_QN_PORT_CACHE.get("expires") or 0):
        return (
            _QN_PORT_CACHE.get("dll_port"),
            _QN_PORT_CACHE.get("pid"),
            str(_QN_PORT_CACHE.get("state") or ""),
        )

    process_map = _ps_process_listen_map("AliWorkbench")
    try:
        cfg_pid = int(configured_pid) if configured_pid not in (None, "") else None
    except (TypeError, ValueError):
        cfg_pid = None
    try:
        cfg_port = int(configured_port) if configured_port not in (None, "") else None
    except (TypeError, ValueError):
        cfg_port = None

    dll_port: Optional[int] = None
    pid: Optional[int] = None
    state = "not_found"
    if cfg_port:
        # Verify configured port still answers; else fall through to discovery
        if _probe_qn_dll_http_port(cfg_port, timeout=0.5):
            dll_port, pid, state = cfg_port, cfg_pid, "configured"
            if pid is None and len(process_map) == 1:
                pid = next(iter(process_map))
        else:
            state = "configured_port_dead"
    if dll_port is None and cfg_pid is not None:
        ports = process_map.get(cfg_pid, [])
        pid = cfg_pid
        for p in ports:
            if _probe_qn_dll_http_port(p):
                dll_port, state = p, "configured_pid_http_ok"
                break
        if dll_port is None:
            state = "configured_pid_no_qn_http"
    if dll_port is None:
        # Process map first, but must HTTP-verify (AliWorkbench may listen unrelated ports)
        for cand_pid, ports in process_map.items():
            for p in ports:
                if _probe_qn_dll_http_port(p):
                    dll_port, pid, state = p, cand_pid, "workbench_http_ok"
                    break
            if dll_port is not None:
                break
    if dll_port is None:
        # Critical: probe AliRender / Injector listen ports (real QN DLL often here)
        p2, pid2, st2 = _http_discover_qn_dll_port()
        if p2:
            dll_port, pid, state = p2, pid2, st2
        elif state in {"not_found", "configured_port_dead"}:
            state = st2 if process_map else "not_found"
        elif process_map and dll_port is None:
            state = "ambiguous_processes_no_qn_http"

    _QN_PORT_CACHE.update(
        expires=now + 15.0,
        dll_port=dll_port,
        pid=pid,
        state=state,
        process_map=process_map,
    )
    return dll_port, pid, state


def _qn_split_buyer(buyer_id: str) -> tuple[str, str]:
    """Return (ccode_or_id, short_numeric_id)."""
    buyer_id = str(buyer_id or "").strip()
    if not buyer_id:
        return "", ""
    if "." in buyer_id:
        short = buyer_id.split(".", 1)[0]
        return buyer_id, short if short.isdigit() else ""
    return buyer_id, buyer_id if buyer_id.isdigit() else ""


def _qn_post(dll_port: int, payload: dict, *, timeout: float = 6.0) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"http://127.0.0.1:{dll_port}/tanyu/client/qn/dll/httpTestApi"
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw_b = resp.read()
        err = _dll_response_error(int(resp.status), raw_b)
        if err:
            return False, err
        return True, raw_b.decode("utf-8", "replace")


def _qn_tail_log(log_dir: str = "", *, max_bytes: int = 200_000) -> str:
    """Read recent 探域 inside log for taobao workbench."""
    from pathlib import Path
    roots = []
    if log_dir:
        roots.append(Path(log_dir))
    roots.append(Path(r"D:\kefuAgent\探域\tanyu2.9.1\logs"))
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(root.glob("inside_*.log"))
    if not files:
        return ""
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = newest.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def _qn_parse_send_result(log_text: str, marker: str) -> str:
    """Return SUCCESS / INVALID_USER / EMPTY_MSG / UNKNOWN from inside log."""
    if not log_text:
        return "UNKNOWN"
    # Prefer last handleMsg_sendMsg block
    idx = log_text.rfind("handleMsg_sendMsg")
    snip = log_text[idx:] if idx >= 0 else log_text[-4000:]
    if marker and marker in snip and "send text message  success" in snip:
        return "SUCCESS"
    if "send text message  success" in snip and (not marker or marker in snip):
        # success for this or recent send
        if marker and marker not in snip:
            # success is for different message
            pass
        else:
            return "SUCCESS"
    if "imServerUser is not valid" in snip:
        return "INVALID_USER"
    if "msg list is empty" in snip:
        return "EMPTY_MSG"
    if "send text message  success" in log_text[-8000:] and marker and marker in log_text[-8000:]:
        return "SUCCESS"
    return "UNKNOWN"


def _qn_open_buyer(
    dll_port: int,
    pid: int,
    *,
    account: str,
    buyer_id: str,
    buyer_nick: str,
    platform_version: str,
) -> None:
    """Best-effort focus conversation (QnMsg 切换客户)."""
    ccode, short = _qn_split_buyer(buyer_id)
    data = {
        "account": account,
        "SellerNick": account,
        "sellerNick": account,
        "BuyerCid": ccode or buyer_id,
        "ccode": ccode or buyer_id,
        "buyerNick": buyer_nick,
        "buyerId": short or buyer_id,
        "userId": short or buyer_id,
        "nick": buyer_nick,
        "openId": short or buyer_id,
        "keyword": buyer_nick or short or buyer_id,
        "sellerId": "",
    }
    for cmd in ("open_buyer", "JsOpenBuyerByOpenId", "add_conversation_from_search", "switch_seller_and_open_buyer"):
        try:
            _qn_post(
                dll_port,
                {
                    "cmd": cmd,
                    "platformType": 0,
                    "platformVersion": platform_version,
                    "processId": pid or 0,
                    "account": account,
                    "data": data,
                    "reqId": f"open-{int(time.time()*1000)}",
                },
                timeout=4.0,
            )
        except Exception:
            continue
        time.sleep(0.25)


def _find_qianniu_hwnd() -> Optional[int]:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or ""
        if "千牛接待台" in title:
            found.append((int(hwnd), title))
        elif title.strip() == "千牛":
            found.append((int(hwnd), title))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    if not found:
        return None
    # Prefer exact 接待台 title
    for hwnd, title in found:
        if "千牛接待台" in title:
            return hwnd
    return found[0][0]


def _set_clipboard_text(text: str) -> None:
    """Set Unicode text to Windows clipboard (robust on 64-bit)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    data = str(text).encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.08)
    else:
        raise RuntimeError("OpenClipboard failed")
    hglob = None
    try:
        user32.EmptyClipboard()
        hglob = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hglob:
            raise RuntimeError("GlobalAlloc failed")
        locked = kernel32.GlobalLock(hglob)
        if not locked:
            raise RuntimeError("GlobalLock failed")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(hglob)
        if not user32.SetClipboardData(CF_UNICODETEXT, hglob):
            raise RuntimeError("SetClipboardData failed")
        # ownership transferred to clipboard
        hglob = None
    finally:
        if hglob:
            kernel32.GlobalFree(hglob)
        user32.CloseClipboard()


def _ui_force_foreground(hwnd: int) -> bool:
    """Bring hwnd to foreground (AttachThreadInput dance)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    attached_fg = False
    attached_tg = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid:
            attached_tg = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        try:
            user32.SetFocus(hwnd)
        except Exception:
            pass
    finally:
        if attached_tg:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.35)
    return int(user32.GetForegroundWindow() or 0) == int(hwnd)


def _ui_send_input_vk(vk: int, *, ctrl: bool = False, alt: bool = False, shift: bool = False) -> None:
    """Send a virtual-key tap once via SendInput.

    Important: do NOT multi-fire (scancode + vk + keybd_event) — that double-sends in 千牛.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("ii", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    MAPVK_VK_TO_VSC = 0
    VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10

    def _send(v: int, up: bool = False) -> None:
        scan = int(user32.MapVirtualKeyW(int(v), MAPVK_VK_TO_VSC) or 0) & 0xFF
        flags = KEYEVENTF_KEYUP if up else 0
        # Single SendInput with both vk + scan (one physical key event)
        inp = INPUT(
            type=INPUT_KEYBOARD,
            ii=INPUT_UNION(ki=KEYBDINPUT(int(v), scan, flags, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    if ctrl:
        _send(VK_CONTROL, False)
    if alt:
        _send(VK_MENU, False)
    if shift:
        _send(VK_SHIFT, False)
    time.sleep(0.01)
    _send(vk, False)
    time.sleep(0.03)
    _send(vk, True)
    time.sleep(0.01)
    if shift:
        _send(VK_SHIFT, True)
    if alt:
        _send(VK_MENU, True)
    if ctrl:
        _send(VK_CONTROL, True)


def _ui_type_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("ii", INPUT_UNION)]

    for ch in text:
        code = ord(ch)
        down = INPUT(type=1, ii=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        up = INPUT(
            type=1,
            ii=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def _ui_click_screen(x: int, y: int) -> None:
    """Click at absolute virtual-desktop pixel (x, y).

    Multi-monitor note: SendInput ABSOLUTE without VIRTUALDESK maps to the
    *primary* monitor only. 千牛 often sits across monitors (negative left),
    so use SetCursorPos + mouse_event which take real virtual-screen coords.
    """
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    # mouse_event is deprecated but reliable for multi-monitor screen coords
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.02)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.02)


def _find_qianniu_workbench_cefs(hwnd: int) -> list[tuple[int, int, int, int]]:
    """Visible CEF panels (usually right-side 工作台 plugins)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    out: list[tuple[int, int, int, int]] = []

    def _cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        if cls.value != "Chrome_RenderWidgetHostHWND":
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(rect)):
            return True
        if rect.right - rect.left < 120 or rect.bottom - rect.top < 120:
            return True
        out.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumChildWindows(hwnd, EnumChildProc(_cb), 0)
    return out


def _find_qianniu_chat_target(hwnd: int) -> tuple[int, int, int, int, int]:
    """Return (0, left, top, right, bottom) for the *native* chat zone.

    千牛 9.x layout (接待台):
    - Left: session list (native)
    - Center: message list + input (native Qt, NOT the 工作台 CEF)
    - Right: 千牛工作台 CEF plugins

    Previous builds wrongly clicked the right CEF workbench, so text never
    entered the chat composer.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ml, mt, mr, mb = rect.left, rect.top, rect.right, rect.bottom

    cefs = _find_qianniu_workbench_cefs(hwnd)
    # Rightmost CEF is the workbench — chat is to its left
    if cefs:
        right_cef_left = max(c[0] for c in cefs)
        chat_right = max(right_cef_left - 8, ml + 200)
    else:
        chat_right = ml + int((mr - ml) * 0.70)

    # Leave room for session list on the left (~160–220px)
    chat_left = ml + 170
    if chat_left >= chat_right - 80:
        chat_left = ml + 80
        chat_right = mr - 40

    # Input sits in the bottom band of the center column
    chat_top = mt + 60
    chat_bottom = mb - 4
    return 0, chat_left, chat_top, chat_right, chat_bottom


def _find_qianniu_chat_panel_rect(hwnd: int) -> tuple[int, int, int, int]:
    _, l, t, r, b = _find_qianniu_chat_target(hwnd)
    return l, t, r, b


def _qn_last_active_buyer_from_logs(
    *,
    account: str = "",
    buyer_id: str = "",
    log_dir: str = "",
) -> dict:
    """Parse latest QnMsg 切换客户 / BuyerNick lines for anti-串台 when CDP is down."""
    from pathlib import Path as _P

    acc = str(account or "").strip()
    bid = str(buyer_id or "").strip()
    roots = []
    if log_dir:
        roots.append(_P(log_dir))
    roots.append(_P(r"D:\kefuAgent\探域\tanyu2.9.1\logs"))
    last: dict = {}
    for root in roots:
        if not root.is_dir():
            continue
        files = sorted(root.glob("QnMsg*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
        files += sorted(root.glob("Injector_cntaobao*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
        for path in files:
            try:
                data = path.read_bytes()
                if len(data) > 300_000:
                    data = data[-300_000:]
                text = data.decode("utf-8", "replace")
            except Exception:
                continue
            for line in reversed(text.splitlines()):
                # BuyerNick=tbxxx BuyerCid=ccode
                if "BuyerNick" in line and "BuyerCid" in line:
                    m_n = re.search(r"BuyerNick\s*=\s*([A-Za-z0-9_\-]+)", line, re.I)
                    m_c = re.search(r"BuyerCid\s*=\s*([^\s]+)", line, re.I)
                    if m_n and m_c:
                        last = {
                            "nick": m_n.group(1),
                            "ccode": m_c.group(1).strip(),
                            "source": path.name,
                            "line": line[-200:],
                        }
                        if (not acc or acc in line) and (not bid or bid in line or bid.split(".", 1)[0] in line):
                            return last
                # 切换客户 account shopId nick uid ccode
                parts = line.split()
                if len(parts) >= 6 and (acc in parts or not acc):
                    # find ccode-like token
                    for i, tok in enumerate(parts):
                        if "@cntaobao" in tok or (tok[:1].isdigit() and "." in tok and "#" in tok):
                            nick = parts[i - 2] if i >= 2 else ""
                            if nick and not nick[:1].isdigit():
                                last = {
                                    "nick": nick,
                                    "ccode": tok,
                                    "source": path.name,
                                    "line": line[-200:],
                                }
                                if not bid or bid in tok or bid.split(".", 1)[0] in line:
                                    return last
                            break
            if last:
                return last
    return last


def _ui_fallback_session_gate(
    buyer_nick: str,
    *,
    buyer_id: str = "",
    account: str = "",
    log_dir: str = "",
) -> tuple[bool, str]:
    """UI paste only when session can be proven = target buyer (CDP preferred, else QnMsg)."""
    nick = _normalize_qn_nick(buyer_nick)
    if not nick:
        return False, "no buyer_nick"
    # 1) CDP (strong)
    port = _discover_qn_cdp_port(force=False)
    if port:
        conn = None
        try:
            conn = _cdp_connect_chat_ws(port)
            ok, cur, raw = _cdp_wait_buyer_session(conn, nick, timeout=2.5)
            if ok:
                return True, f"cdp_session_ok={cur}"
            return False, f"cdp_session_mismatch want={nick} got={cur} raw={str(raw)[:160]}"
        except Exception as exc:
            # fall through to log gate
            cdp_err = str(exc)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    else:
        cdp_err = "no cdp"
    # 2) QnMsg / Injector last active buyer (weaker, but better than blind paste)
    info = _qn_last_active_buyer_from_logs(account=account, buyer_id=buyer_id, log_dir=log_dir)
    if not info:
        return False, f"{cdp_err}; no QnMsg switch log for session gate"
    log_nick = _normalize_qn_nick(info.get("nick"))
    log_ccode = str(info.get("ccode") or "")
    bid = str(buyer_id or "")
    nick_ok = bool(log_nick and log_nick.lower() == nick.lower())
    ccode_ok = bool(bid and (bid == log_ccode or bid in log_ccode or log_ccode in bid))
    if nick_ok or ccode_ok:
        return True, f"log_session_ok nick={log_nick} ccode={log_ccode} via={info.get('source')}"
    return False, (
        f"{cdp_err}; log_session_mismatch want={nick}/{bid} "
        f"got={log_nick}/{log_ccode} via={info.get('source')}"
    )


def _ui_post_enter(hwnd: int) -> None:
    """Post WM_KEYDOWN/UP Enter to target hwnd (backup when SendInput is ignored)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    VK_RETURN = 0x0D
    # lParam: repeat=1, scan=0x1C (Enter)
    lparam_down = 0x001C0001
    lparam_up = 0xC01C0001
    try:
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, lparam_down)
        time.sleep(0.03)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, lparam_up)
    except Exception:
        pass
    # Also to foreground thread focus child if any
    try:
        fg = user32.GetForegroundWindow()
        if fg and int(fg) != int(hwnd):
            user32.PostMessageW(fg, WM_KEYDOWN, VK_RETURN, lparam_down)
            user32.PostMessageW(fg, WM_KEYUP, VK_RETURN, lparam_up)
    except Exception:
        pass


def _ui_send_qianniu(content: str) -> tuple[bool, str]:
    """Legacy mouse/keyboard paste path — intentionally minimal and rarely used.

    openbot-style send must NOT use this. Kept only for explicit allow_ui_fallback.
    One focus click, Ctrl+V once, Enter once — no multi-point mouse spray.
    """
    if sys.platform != "win32":
        return False, "ui send only on Windows"
    import ctypes

    user32 = ctypes.windll.user32
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass

    hwnd = _find_qianniu_hwnd()
    if not hwnd:
        return False, "未找到「千牛接待台」窗口"
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)

    _, left, top, right, bottom = _find_qianniu_chat_target(hwnd)
    width = max(1, right - left)
    vx = int(user32.GetSystemMetrics(76))
    vy = int(user32.GetSystemMetrics(77))
    vw = max(int(user32.GetSystemMetrics(78)), 1)
    vh = max(int(user32.GetSystemMetrics(79)), 1)
    x0 = max(vx + 2, min(left + int(width * 0.48), vx + vw - 3))
    y0 = max(vy + 2, min(bottom - 42, vy + vh - 3))

    paste_ok = False
    try:
        _set_clipboard_text(content)
        paste_ok = True
    except Exception as exc:
        return False, f"clipboard fail: {exc}"

    _ui_click_screen(x0, y0)
    time.sleep(0.12)
    _ui_send_input_vk(0x41, ctrl=True)  # Ctrl+A
    time.sleep(0.04)
    _ui_send_input_vk(0x56, ctrl=True)  # Ctrl+V
    time.sleep(0.25)
    _ui_send_input_vk(0x0D)  # Enter once only
    time.sleep(0.2)
    return True, f"ui_minimal hwnd={hwnd};paste={paste_ok};click={x0},{y0};enter=1"


def read_qn_login_status(log_dir: str = "") -> dict:
    """Parse latest SmartRobot qnAccountList for ifLogin flags."""
    from pathlib import Path
    import re

    roots = []
    if log_dir:
        roots.append(Path(log_dir))
    roots.append(Path(r"D:\kefuAgent\探域\tanyu2.9.1\logs"))
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(root.glob("SmartRobot*.log*"))
    if not files:
        return {"ok": False, "accounts": [], "any_logged_in": False}
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = newest.read_bytes()[-600_000:].decode("utf-8", "replace")
    except Exception:
        return {"ok": False, "accounts": [], "any_logged_in": False}
    accounts = []
    for m in re.finditer(
        r'\{\s*"platformType"\s*:\s*0\s*,\s*"account"\s*:\s*"([^"]+)"\s*,\s*"receptionIsOpen"\s*:\s*(true|false)\s*,\s*"ifLogin"\s*:\s*(true|false)',
        data,
    ):
        accounts.append(
            {
                "account": m.group(1),
                "reception_open": m.group(2) == "true",
                "if_login": m.group(3) == "true",
            }
        )
    # unique by account keep last
    by_acc = {}
    for row in accounts:
        by_acc[row["account"]] = row
    rows = list(by_acc.values())
    return {
        "ok": True,
        "accounts": rows,
        "any_logged_in": any(r.get("if_login") for r in rows),
        "source": str(newest),
    }


def _netstat_listen_ports_for_pids(pids: list[int]) -> Dict[int, List[int]]:
    """Listen ports for given PIDs. Prefer psutil (no subprocess hang)."""
    if not pids:
        return {}
    want = {int(p) for p in pids}
    result: Dict[int, List[int]] = {}
    try:
        import psutil  # type: ignore

        for pid in want:
            try:
                proc = psutil.Process(pid)
                for c in proc.net_connections(kind="inet"):
                    if c.status == psutil.CONN_LISTEN and c.laddr:
                        port = int(getattr(c.laddr, "port", 0) or 0)
                        if port:
                            result.setdefault(pid, []).append(port)
            except (psutil.Error, Exception):
                continue
        if result:
            return {p: sorted(set(ports)) for p, ports in result.items()}
    except Exception:
        pass
    # fallback netstat
    try:
        raw = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            stderr=subprocess.DEVNULL,
            timeout=3,
            **_hidden_subprocess_kwargs(),
        )
        out = raw.decode("gbk", "replace")
    except Exception:
        return {}
    for line in out.splitlines():
        up = line.upper()
        if "LISTENING" not in up and "侦听" not in line and "LISTEN" not in up:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid not in want:
            continue
        local = parts[1] if len(parts) >= 2 else ""
        if ":" not in local:
            continue
        port_s = local.rsplit(":", 1)[-1]
        if not port_s.isdigit():
            continue
        result.setdefault(pid, []).append(int(port_s))
    return {p: sorted(set(ports)) for p, ports in result.items()}


_CDP_PORT_CACHE: Dict[str, Any] = {"expires": 0.0, "port": None}


def _discover_qn_cdp_port(*, force: bool = False) -> Optional[int]:
    """Find AliRender CEF remote-debugging port that hosts web_chat recent.html.

    Must stay fast: GUI calls this on the UI thread. No PowerShell loops.
    """
    now = time.time()
    if not force and now < float(_CDP_PORT_CACHE.get("expires") or 0):
        return _CDP_PORT_CACHE.get("port")

    candidates: list[int] = [11537, 9222, 9333]
    try:
        pids = _pids_by_process_name("AliRender") + _pids_by_process_name("AliWorkbench")
        port_map = _netstat_listen_ports_for_pids(pids)
        for ports in port_map.values():
            candidates.extend(ports)
    except Exception:
        pass
    # 探域 Injector often logs: 新增了cdp连接: ws://127.0.0.1:11537/devtools/...
    try:
        from pathlib import Path as _P

        log_root = _P(r"D:\kefuAgent\探域\tanyu2.9.1\logs")
        if log_root.is_dir():
            for path in sorted(log_root.glob("Injector_cntaobao*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
                try:
                    chunk = path.read_bytes()[-120_000:].decode("utf-8", "replace")
                except Exception:
                    continue
                for m in re.finditer(r"127\.0\.0\.1:(\d{2,5})/devtools", chunk):
                    try:
                        candidates.append(int(m.group(1)))
                    except ValueError:
                        pass
                for m in re.finditer(r"cdp\s*端口号[:：\s]+(\d{2,5})", chunk, re.I):
                    try:
                        candidates.append(int(m.group(1)))
                    except ValueError:
                        pass
    except Exception:
        pass

    found: Optional[int] = None
    seen = set()
    # Prefer known CDP ports first; only probe a few to keep status <1s
    ordered: list[int] = []
    for preferred in (11537, 9222, 9333):
        if preferred in candidates:
            ordered.append(preferred)
    for port in candidates:
        try:
            pi = int(port)
        except (TypeError, ValueError):
            continue
        if pi and pi not in ordered:
            ordered.append(pi)
    for port_i in ordered[:12]:
        if port_i in seen:
            continue
        seen.add(port_i)
        try:
            with urlrequest.urlopen(f"http://127.0.0.1:{port_i}/json/list", timeout=0.2) as resp:
                tabs = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(tabs, list):
                continue
            for t in tabs:
                url = str(t.get("url") or "")
                title = str(t.get("title") or "")
                if "web_chat" in url or "recent.html" in url or "消息聊天" in title:
                    found = port_i
                    break
            if found is not None:
                break
        except Exception:
            continue

    _CDP_PORT_CACHE.update(expires=now + 12.0, port=found)
    return found


_QN_NICK_CACHE: Dict[str, str] = {}


def _qn_lookup_nick_from_logs(buyer_id: str, log_dir: str = "") -> str:
    """Best-effort: QnMsg/Injector logs often have BuyerNick for a ccode."""
    from pathlib import Path as _P

    bid = str(buyer_id or "").strip()
    if not bid:
        return ""
    if bid in _QN_NICK_CACHE:
        return _QN_NICK_CACHE[bid]
    short = bid.split(".", 1)[0] if "." in bid else bid
    roots = []
    if log_dir:
        roots.append(_P(log_dir))
    roots.append(_P(r"D:\kefuAgent\探域\tanyu2.9.1\logs"))

    def _accept(nick: str) -> str:
        n = str(nick or "").strip()
        if not n or n in {short, bid} or "@" in n:
            return ""
        if n[:1].isdigit() and ("." in n or len(n) >= 8):
            return ""
        _QN_NICK_CACHE[bid] = n
        return n

    for root in roots:
        if not root.is_dir():
            continue
        files = []
        for pat in ("QnMsg*.log", "Injector_cntaobao*.log", "inside_*.log", "SmartRobot_*.log"):
            files.extend(root.glob(pat))
        files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:12]
        for path in files:
            try:
                data = path.read_bytes()
                if len(data) > 400_000:
                    data = data[-400_000:]
                text = data.decode("utf-8", "replace")
            except Exception:
                continue
            if bid not in text and short not in text:
                continue
            for line in reversed(text.splitlines()):
                if bid not in line and short not in line:
                    continue
                m = re.search(r"BuyerNick\s*=\s*([A-Za-z0-9_\-]+)", line, re.I)
                if m and bid in line:
                    got = _accept(m.group(1))
                    if got:
                        return got
                m = re.search(r'"buyerNick"\s*:\s*"([A-Za-z0-9_\-]+)"', line, re.I)
                if m and (bid in line or short in line):
                    got = _accept(m.group(1))
                    if got:
                        return got
                # 切换客户 sbpgklso 11789284 tb136202715 4054500565 ccode
                parts = line.split()
                if len(parts) >= 6 and bid in parts:
                    try:
                        idx = parts.index(bid)
                        if idx >= 2:
                            got = _accept(parts[idx - 2])
                            if got:
                                return got
                    except ValueError:
                        pass
    return ""


def _qn_buyer_nick_for_im(buyer_id: str, buyer_nick: str, *, log_dir: str = "") -> str:
    """Normalize buyer nick for application.openChat / insertText2Inputbox."""
    nick = str(buyer_nick or "").strip()
    if nick:
        if nick.startswith("cntaobao"):
            return nick[len("cntaobao") :]
        # ignore ccode-like nicks
        if "@cntaobao" not in nick and not (nick[:1].isdigit() and "." in nick):
            return nick
    # fallback: sometimes buyer_id is pure nick
    bid = str(buyer_id or "").strip()
    if bid and not bid[0].isdigit() and "@" not in bid and "." not in bid:
        return bid
    # last resort: 探域 logs
    return _qn_lookup_nick_from_logs(bid, log_dir)


def _cdp_connect_chat_ws(port: int):
    """Connect to 千牛消息聊天 page CDP websocket (suppress Origin like Chromium requires)."""
    import websocket  # type: ignore

    tabs = json.loads(
        urlrequest.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3).read().decode("utf-8", "replace")
    )
    chat = None
    for t in tabs:
        url = str(t.get("url") or "")
        if "web_chat" in url or "recent.html" in url:
            chat = t
            break
    if not chat or not chat.get("webSocketDebuggerUrl"):
        raise RuntimeError("未找到千牛消息聊天 CDP page (web_chat-packer/recent.html)")
    return websocket.create_connection(chat["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)


def _cdp_eval(conn, expression: str, *, await_promise: bool = True, wait: float = 8.0):
    import websocket  # type: ignore

    req_id = int(time.time() * 1000) % 1_000_000_000
    conn.send(
        json.dumps(
            {
                "id": req_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": True,
                    "userGesture": True,
                },
            }
        )
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        raw = conn.recv()
        data = json.loads(raw)
        if data.get("id") != req_id:
            continue
        res = data.get("result") or {}
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res.get("exceptionDetails"))[:300])
        val = (res.get("result") or {}).get("value")
        return val
    raise TimeoutError("CDP evaluate timeout")


def _cdp_imsdk_invoke(conn, api: str, param: dict, *, wait: float = 8.0):
    """imsdk.invoke via CDP — same channel openbot uses after inject/WebSocket bridge."""
    expr = f"""
(async () => {{
  try {{
    const r = await Promise.race([
      window.imsdk.invoke({json.dumps(api)}, {json.dumps(param, ensure_ascii=False)}, 5000),
      new Promise((_, rej) => setTimeout(() => rej(new Error('imsdk.invoke timeout')), 5500)),
    ]);
    return {{ ok: true, r }};
  }} catch (e) {{
    return {{ ok: false, err: String(e) }};
  }}
}})()
"""
    return _cdp_eval(conn, expr, await_promise=True, wait=wait)


def _normalize_qn_nick(value: Any) -> str:
    nick = str(value or "").strip()
    if nick.startswith("cntaobao"):
        nick = nick[len("cntaobao") :]
    # drop ccode-like
    if "@cntaobao" in nick or ("." in nick and nick[:1].isdigit()):
        return ""
    return nick


def _cdp_extract_current_buyer_nick(payload: Any) -> str:
    """Pull buyer nick from im.uiutil.GetCurrentConversationID (and similar) result."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return _normalize_qn_nick(payload)
    if not isinstance(payload, dict):
        return ""
    # unwrap {ok,r} from our invoke helper
    if "r" in payload and isinstance(payload.get("r"), (dict, str)):
        return _cdp_extract_current_buyer_nick(payload.get("r"))
    # common shapes
    for key in (
        "nick",
        "buyerNick",
        "display",
        "displayName",
        "targetNick",
        "contactNick",
        "uid",
        "userNick",
    ):
        n = _normalize_qn_nick(payload.get(key))
        if n:
            return n
    for nest in ("conversation", "cid", "target", "user", "contact", "result", "data"):
        nested = payload.get(nest)
        if isinstance(nested, (dict, str)):
            n = _cdp_extract_current_buyer_nick(nested)
            if n:
                return n
    # deep-ish scan for nick fields
    try:
        raw = json.dumps(payload, ensure_ascii=False)
        m = re.search(r'"(?:nick|buyerNick|displayName)"\s*:\s*"([^"]+)"', raw)
        if m:
            return _normalize_qn_nick(m.group(1))
        m = re.search(r"cntaobao([A-Za-z0-9_\-]+)", raw)
        if m:
            return _normalize_qn_nick(m.group(1))
    except Exception:
        pass
    return ""


def _cdp_get_current_buyer_nick(conn) -> tuple[str, Any]:
    """Return (nick, raw) from im.uiutil.GetCurrentConversationID — openbot uses this."""
    raw = _cdp_imsdk_invoke(conn, "im.uiutil.GetCurrentConversationID", {}, wait=5.0)
    nick = _cdp_extract_current_buyer_nick(raw)
    return nick, raw


def _cdp_wait_buyer_session(conn, target_nick: str, *, timeout: float = 5.0) -> tuple[bool, str, Any]:
    """Like openbot: wait until current conversation buyer matches target nick."""
    want = _normalize_qn_nick(target_nick)
    if not want:
        return False, "", None
    deadline = time.time() + max(0.5, float(timeout))
    last_raw: Any = None
    last_nick = ""
    while time.time() < deadline:
        try:
            last_nick, last_raw = _cdp_get_current_buyer_nick(conn)
        except Exception as exc:
            last_raw = {"err": str(exc)}
            last_nick = ""
        if last_nick and last_nick.lower() == want.lower():
            return True, last_nick, last_raw
        # sometimes uid is returned without nick — match ccode short forms later
        time.sleep(0.2)
    return False, last_nick, last_raw


def _uia_find_chat_composer() -> tuple[list, list, str]:
    """Return (edits, send_buttons, err). Requires 千牛无障碍."""
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:
        return [], [], f"no uiautomation: {exc}"

    auto.SetGlobalSearchTimeout(2.5)
    targets = []
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                cls = w.ClassName or ""
                name = w.Name or ""
                if (
                    cls == "MutilChatView"
                    or "千牛接待台" in name
                    or "接待台" in name
                    or (cls.startswith("Qt") and "千牛" in name)
                ):
                    targets.append(w)
            except Exception:
                continue
        if not targets:
            for c, _d in auto.WalkControl(auto.GetRootControl(), maxDepth=6):
                try:
                    if (c.ClassName or "") == "TextRichEdit":
                        top = c.GetTopLevelControl() if hasattr(c, "GetTopLevelControl") else None
                        if top is not None:
                            targets.append(top)
                        break
                except Exception:
                    continue
    except Exception as exc:
        return [], [], f"uia enum fail: {exc}"

    if not targets:
        return [], [], "未找到千牛接待台窗口"

    edits: list = []
    btns: list = []
    for w in targets:
        try:
            for c, _depth in auto.WalkControl(w, maxDepth=40):
                try:
                    cn = (c.ClassName or "")
                    nm = (c.Name or "").strip()
                    ct = c.ControlTypeName or ""
                    if cn == "TextRichEdit":
                        edits.append(c)
                    # Exact 「发送」 only — skip 发送方式 / dropdown / menu items
                    if nm != "发送":
                        continue
                    if "Menu" in ct or "Combo" in ct or "List" in ct:
                        continue
                    # Prefer plain Button; still collect SplitButton but rank lower later
                    if "Button" in ct:
                        btns.append(c)
                except Exception:
                    continue
        except Exception:
            continue
    return edits, btns, ""


def _uia_pick_main_send_button(btns: list):
    """Pick the real 发送 key, not the ▼ dropdown next to it.

    千牛 often has split control: main 「发送」 + small triangle for Enter/Ctrl+Enter menu.
    Prefer wider buttons / plain Button; avoid tiny width chevrons.
    """
    if not btns:
        return None
    scored = []
    for b in btns:
        try:
            ct = b.ControlTypeName or ""
            rect = b.BoundingRectangle
            w = max(0, int(rect.right - rect.left))
            h = max(0, int(rect.bottom - rect.top))
            # tiny width ≈ dropdown chevron
            if w > 0 and w < 28:
                continue
            score = w * h
            if "Split" in ct:
                score *= 0.3
            if ct == "ButtonControl" or ct.endswith("Button"):
                score *= 1.5
            scored.append((score, w, h, b))
        except Exception:
            scored.append((1, 0, 0, b))
    if not scored:
        # fall back to first non-filtered
        return btns[0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][3]


def _uia_focus_composer_and_keys(*key_seqs: str) -> str:
    """Focus TextRichEdit then SendKeys — no mouse. key_seqs like '{Enter}'."""
    edits, _btns, err = _uia_find_chat_composer()
    if err and not edits:
        return f"focus_fail:{err}"
    if not edits:
        return "no_edit"
    edit = edits[-1]
    try:
        edit.SetFocus()
        time.sleep(0.08)
    except Exception as exc:
        return f"setfocus_err:{exc}"
    detail = ["focus_edit"]
    for seq in key_seqs:
        try:
            edit.SendKeys(seq)
            detail.append(seq)
            time.sleep(0.12)
        except Exception as exc:
            detail.append(f"{seq}_err={exc}")
    return "+".join(detail)


def _uia_click_send_button() -> tuple[bool, str]:
    """Activate 发送 without hitting the dropdown triangle.

    Prefer Invoke / keyboard; if must click, click left side of the main 发送 button.
    """
    edits, btns, err = _uia_find_chat_composer()
    if err and not edits and not btns:
        return False, err
    if not edits and not btns:
        return False, "无 TextRichEdit/发送（请开无障碍）"

    try:
        # Close any open dropdown from a previous bad click
        try:
            _ui_send_input_vk(0x1B)  # Escape
            time.sleep(0.08)
        except Exception:
            pass

        if edits:
            try:
                edits[-1].SetFocus()
                time.sleep(0.06)
            except Exception:
                pass

        btn = _uia_pick_main_send_button(btns)
        if btn is not None:
            # 1) Invoke — no mouse
            try:
                btn.GetInvokePattern().Invoke()
                time.sleep(0.2)
                return True, "uia_invoke_main_send"
            except Exception:
                pass
            # 2) keyboard on focused edit
            if edits:
                try:
                    edits[-1].SendKeys("{Enter}")
                    time.sleep(0.2)
                    return True, "uia_edit_enter"
                except Exception:
                    pass
            # 3) Click LEFT side of 发送 (ratioX=0.25) — avoid right-side ▼
            try:
                btn.Click(ratioX=0.22, ratioY=0.5, simulateMove=False, waitTime=0.15)
                time.sleep(0.15)
                return True, "uia_click_send_left"
            except Exception:
                try:
                    rect = btn.BoundingRectangle
                    x = int(rect.left + max(8, (rect.right - rect.left) * 0.25))
                    y = int(rect.top + (rect.bottom - rect.top) * 0.5)
                    _ui_click_screen(x, y)
                    time.sleep(0.2)
                    return True, f"uia_click_send_xy={x},{y}"
                except Exception as exc:
                    return False, f"click_fail:{exc}"

        if edits:
            edits[-1].SendKeys("{Enter}")
            time.sleep(0.2)
            return True, "uia_edit_enter_only"
    except Exception as exc:
        return False, f"uia send fail: {exc}"
    return False, "uia no action"


def _imsdk_is_input_empty(empty_res: Any) -> bool:
    if not isinstance(empty_res, dict):
        return True
    if "isEmpty" in empty_res:
        return bool(empty_res.get("isEmpty"))
    for key in ("result", "r"):
        nested = empty_res.get(key)
        if isinstance(nested, dict) and "isEmpty" in nested:
            return bool(nested.get("isEmpty"))
    return True


def _submit_after_insert_text(*, prefer_enter: bool = True) -> tuple[bool, str]:
    """After insertText2Inputbox: keyboard-first submit; never aim at 发送▼ dropdown."""
    actions: list[str] = []
    # Always dismiss accidental dropdown first
    try:
        _ui_send_input_vk(0x1B)
        time.sleep(0.05)
        actions.append("esc")
    except Exception:
        pass

    if prefer_enter:
        # Focus composer then Enter / Ctrl+Enter via UIA SendKeys (targets edit, not dropdown)
        d1 = _uia_focus_composer_and_keys("{Enter}")
        actions.append(d1)
        time.sleep(0.25)
        return True, "+".join(actions)

    # Secondary: Ctrl+Enter (for 回车换行 accounts)
    d2 = _uia_focus_composer_and_keys("^{Enter}")
    actions.append(d2)
    time.sleep(0.2)
    # Then Invoke main 发送 (not triangle)
    uia_ok, uia_detail = _uia_click_send_button()
    actions.append(f"uia={uia_ok}:{uia_detail}")
    return uia_ok or True, "+".join(actions)


def send_text_qianniu_openbot_ws(
    buyer_id: str,
    content: str,
    *,
    buyer_nick: str = "",
) -> dict:
    """Send via openbot-compatible local WebSocket bridge (no 探域 DevTools).

    Requires: recent.html inject + Agent listening on ws://127.0.0.1:41010
    + 千牛接待台聊天页已打开（脚本会连上 Agent）。
    """
    nick = _qn_buyer_nick_for_im(buyer_id, buyer_nick)
    if not nick:
        return {
            "ok": False,
            "status": "failed",
            "error": "buyer_nick required",
            "error_user": "缺少买家旺旺昵称，无法 openChat",
            "via": "openbot_ws",
            "real_send": False,
        }
    try:
        from .openbot_ws import eval_expression, invoke_imsdk, is_client_connected, start_openbot_bridge
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"openbot_ws import: {exc}",
            "error_user": f"openbot 桥模块加载失败：{exc}",
            "via": "openbot_ws",
            "real_send": False,
        }

    start_openbot_bridge()
    if not is_client_connected():
        return {
            "ok": False,
            "status": "failed",
            "error": "no openbot ws client",
            "error_user": (
                "千牛聊天页未连上 Agent（ws://127.0.0.1:41010）。"
                "请确认：①已注入桥接脚本 ②重启过千牛 ③已打开接待台聊天窗口。"
            ),
            "via": "openbot_ws",
            "real_send": False,
            "buyer_nick": nick,
        }

    try:
        open_res = invoke_imsdk("application.openChat", {"nick": f"cntaobao{nick}"}, timeout=10.0)
        time.sleep(0.45)
        # best-effort current conversation check
        try:
            conv = invoke_imsdk("im.uiutil.GetCurrentConversationID", {}, timeout=5.0)
            cur = _cdp_extract_current_buyer_nick(conv)
            if cur and cur.lower() != nick.lower():
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"session mismatch want={nick} got={cur}",
                    "error_user": (
                        f"已拒绝发送（防串台）：当前会话是 {cur}，不是 {nick}。"
                        "请确认 openChat 是否成功。"
                    ),
                    "via": "openbot_ws",
                    "real_send": False,
                    "buyer_nick": nick,
                    "current_nick": cur,
                    "open": open_res,
                    "conversation": conv,
                }
        except Exception:
            conv = None
            cur = ""

        ins_res = invoke_imsdk(
            "application.insertText2Inputbox",
            {"uid": f"cntaobao{nick}", "text": content},
            timeout=10.0,
        )
        time.sleep(0.3)
        empty_res = invoke_imsdk("application.isInputboxEmpty", {}, timeout=5.0)
        is_empty = _imsdk_is_input_empty(empty_res)
        if is_empty:
            return {
                "ok": False,
                "status": "failed",
                "error": f"insert empty open={open_res} ins={ins_res} empty={empty_res}",
                "error_user": "文字未进入输入框（insertText2Inputbox 无效）",
                "via": "openbot_ws",
                "real_send": False,
                "buyer_nick": nick,
                "open": open_res,
                "insert": ins_res,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"openbot_ws invoke fail: {exc}",
            "error_user": f"openbot 桥调用失败：{exc}",
            "via": "openbot_ws",
            "real_send": False,
            "buyer_nick": nick,
        }

    # Submit: Enter first (no mouse). openbot 源码默认 FlaUI Click 发送会动鼠标。
    send_detail = ""
    cleared = False

    def _check_cleared() -> bool:
        try:
            empty_x = invoke_imsdk("application.isInputboxEmpty", {}, timeout=4.0)
        except Exception:
            return False
        if not isinstance(empty_x, dict):
            return False
        return _imsdk_is_input_empty(empty_x)

    try:
        # 1) Focus 输入框 + Enter（不动鼠标去点发送旁的▼）
        _ok_sub, send_detail = _submit_after_insert_text(prefer_enter=True)
        time.sleep(0.35)
        cleared = _check_cleared()
        # 2) Ctrl+Enter + Invoke 主「发送」（点左侧，避开下拉三角）
        if not cleared:
            _ok2, d2 = _submit_after_insert_text(prefer_enter=False)
            send_detail = f"{send_detail}|retry={d2}"
            time.sleep(0.35)
            cleared = _check_cleared()
        # 3) If menu was opened by mistake, Esc and Ctrl+Enter once more
        if not cleared:
            try:
                _ui_send_input_vk(0x1B)
                time.sleep(0.1)
                d3 = _uia_focus_composer_and_keys("^{Enter}", "{Enter}")
                send_detail = f"{send_detail}|esc_retry={d3}"
                time.sleep(0.35)
                cleared = _check_cleared()
            except Exception as exc:
                send_detail = f"{send_detail}|esc_retry_err={exc}"
    except Exception as exc:
        send_detail = f"{send_detail}|err={exc}"
        cleared = False

    return {
        "ok": True,
        "status": "accepted",
        "via": "openbot_ws",
        "real_send": bool(cleared),
        "inserted": True,
        "sent_click": True,
        "session_verified": True,
        "input_cleared": cleared,
        "buyer_nick": nick,
        "submit": send_detail,
        "note": (
            f"openbot 桥：insertText → {nick} → 回车发送"
            + ("，输入框已清空" if cleared else "；请确认气泡")
        ),
        "error_user": "" if cleared else "已尝试发送，请到千牛确认气泡",
    }


def send_text_qianniu_openbot(
    buyer_id: str,
    content: str,
    *,
    buyer_nick: str = "",
) -> dict:
    """Send like openbot: openChat → verify current buyer → insertText → UIA 发送.

    Prefer openbot WebSocket inject bridge (no 探域). Fall back to DevTools CDP if present.

    Anti-串台 (same as openbot QNRpa.OpenAndSendText):
    - Never insert/send unless GetCurrentConversationID nick matches target.
    """
    # ---- 1) openbot WS inject bridge (same as GitHub openbot, no 探域) ----
    openbot_ws_fail: dict = {}
    try:
        ws_res = send_text_qianniu_openbot_ws(buyer_id, content, buyer_nick=buyer_nick)
        if isinstance(ws_res, dict):
            if ws_res.get("ok") and (ws_res.get("real_send") or ws_res.get("sent_click")):
                return ws_res
            if ws_res.get("inserted") and not ws_res.get("ok"):
                return ws_res
            # Connected but failed (session/insert/send) → return, do not mouse thrash
            if "no openbot ws client" not in str(ws_res.get("error") or ""):
                return ws_res
            openbot_ws_fail = ws_res
        else:
            openbot_ws_fail = {"error": "ws path non-dict"}
    except Exception as exc:
        openbot_ws_fail = {"error": f"ws path exc: {exc}"}

    nick = _qn_buyer_nick_for_im(buyer_id, buyer_nick)
    if not nick:
        return {
            "ok": False,
            "status": "failed",
            "error": "buyer_nick required for openbot path",
            "error_user": "缺少买家旺旺昵称，无法 openChat/insertText2Inputbox",
            "via": "openbot_ws",
            "real_send": False,
            "openbot_ws": openbot_ws_fail,
        }

    port = _discover_qn_cdp_port(force=True)
    if not port:
        return {
            "ok": False,
            "status": "failed",
            "error": "openbot_ws not connected and no devtools cdp",
            "error_user": (
                "openbot 桥未连上（千牛聊天页未连接 Agent）。"
                "请：①管理员运行 Agent 注入桥接 ②完全重启千牛 ③打开接待台聊天窗口；"
                "无需探域。详情："
                + str(openbot_ws_fail.get("error_user") or openbot_ws_fail.get("error") or "")
            ),
            "via": "openbot_ws",
            "real_send": False,
            "buyer_nick": nick,
            "openbot_ws": openbot_ws_fail,
        }

    conn = None
    open_res: Any = None
    ins_res: Any = None
    empty_res: Any = None
    conv_raw: Any = None
    cur_nick = ""
    is_empty = True
    try:
        conn = _cdp_connect_chat_ws(port)
        # 1) open conversation (openbot)
        open_res = _cdp_imsdk_invoke(conn, "application.openChat", {"nick": f"cntaobao{nick}"})
        time.sleep(0.35)
        # 2) wait until current session is THIS buyer — refuse if not (anti-串台)
        ok_sess, cur_nick, conv_raw = _cdp_wait_buyer_session(conn, nick, timeout=5.0)
        if not ok_sess:
            return {
                "ok": False,
                "status": "failed",
                "error": f"session mismatch after openChat want={nick} got={cur_nick}",
                "error_user": (
                    f"已拒绝发送（防串台）：未能确认当前会话是买家 {nick}"
                    + (f"（当前={cur_nick}）" if cur_nick else "（读不到当前会话）")
                    + "。请在千牛点开该买家会话后重试。"
                ),
                "via": "openbot_cdp",
                "real_send": False,
                "cdp_port": port,
                "buyer_nick": nick,
                "current_nick": cur_nick,
                "open": open_res,
                "conversation": conv_raw,
            }
        # 3) insert only after session verified
        ins_res = _cdp_imsdk_invoke(
            conn,
            "application.insertText2Inputbox",
            {"uid": f"cntaobao{nick}", "text": content},
        )
        time.sleep(0.25)
        # re-check session once more right before send click
        ok_sess2, cur_nick2, conv_raw2 = _cdp_wait_buyer_session(conn, nick, timeout=1.5)
        if not ok_sess2:
            return {
                "ok": False,
                "status": "failed",
                "error": f"session changed before send want={nick} got={cur_nick2}",
                "error_user": (
                    f"已拒绝发送（防串台）：写字后会话已不是 {nick}"
                    + (f"（当前={cur_nick2}）" if cur_nick2 else "")
                    + "，未点发送。"
                ),
                "via": "openbot_cdp",
                "real_send": False,
                "cdp_port": port,
                "buyer_nick": nick,
                "current_nick": cur_nick2,
                "inserted_maybe": True,
                "open": open_res,
                "insert": ins_res,
                "conversation": conv_raw2,
            }
        cur_nick = cur_nick2 or cur_nick
        conv_raw = conv_raw2 or conv_raw
        empty_res = _cdp_imsdk_invoke(conn, "application.isInputboxEmpty", {})
        is_empty = True
        if isinstance(empty_res, dict):
            r = empty_res.get("r") if "r" in empty_res else empty_res
            if isinstance(r, dict) and "isEmpty" in r:
                is_empty = bool(r.get("isEmpty"))
            elif empty_res.get("ok") and isinstance(empty_res.get("r"), dict):
                is_empty = bool(empty_res["r"].get("isEmpty", True))
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"openbot cdp fail: {exc}",
            "error_user": f"千牛 CDP 插入失败：{exc}",
            "via": "openbot_cdp",
            "real_send": False,
            "cdp_port": port,
            "buyer_nick": nick,
        }
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    if is_empty:
        return {
            "ok": False,
            "status": "failed",
            "error": f"insertText2Inputbox did not fill input open={open_res} ins={ins_res} empty={empty_res}",
            "error_user": "文字未进入千牛输入框（insertText2Inputbox 无效）",
            "via": "openbot_cdp",
            "real_send": False,
            "cdp_port": port,
            "buyer_nick": nick,
            "current_nick": cur_nick,
            "open": open_res,
            "insert": ins_res,
        }

    # Text is in box AND session verified — openbot style: UIA 发送 once, else Enter once
    # No mouse thrashing / multi-click (that is NOT openbot).
    uia_ok, uia_detail = _uia_click_send_button()
    if not uia_ok:
        try:
            # keyboard only — no SetCursorPos spray
            _ui_send_input_vk(0x0D)
            time.sleep(0.2)
            uia_detail = f"{uia_detail};enter_fallback"
            uia_ok = True  # attempted; verify via input_cleared below
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": f"text in input but send click failed: {uia_detail}; enter_err={exc}",
                "error_user": (
                    "文字已写入输入框，但未能点「发送」。"
                    "请在千牛打开「无障碍模式」（多账号接待 / MutilChatView）后重试。"
                ),
                "via": "openbot_cdp",
                "real_send": False,
                "cdp_port": port,
                "inserted": True,
                "session_verified": True,
                "buyer_nick": nick,
                "current_nick": cur_nick,
                "uia": uia_detail,
                "open": open_res,
                "insert": ins_res,
            }

    # Verify input cleared when possible
    cleared = False
    try:
        conn2 = _cdp_connect_chat_ws(port)
        empty2 = _cdp_imsdk_invoke(conn2, "application.isInputboxEmpty", {})
        conn2.close()
        if isinstance(empty2, dict):
            r2 = empty2.get("r") if "r" in empty2 else empty2
            if isinstance(r2, dict):
                cleared = bool(r2.get("isEmpty"))
    except Exception:
        cleared = False

    return {
        "ok": True,
        "status": "accepted",
        "via": "openbot_cdp",
        "real_send": bool(cleared),
        "cdp_port": port,
        "inserted": True,
        "sent_click": True,
        "session_verified": True,
        "buyer_nick": nick,
        "current_nick": cur_nick,
        "input_cleared": cleared,
        "uia": uia_detail,
        "note": (
            f"openbot 路径：已确认会话={nick}，insertText2Inputbox + 点击发送"
            + ("，输入框已清空" if cleared else "；请确认气泡（输入框未确认清空）")
        ),
        "error_user": (
            ""
            if cleared
            else "已尝试发送，请到千牛确认气泡；若没有，请开启千牛无障碍模式后重试"
        ),
    }



def send_text_qianniu(
    buyer_id: str,
    content: str,
    account: str,
    *,
    buyer_nick: str = "",
    platform_version: str = "9.77.01N",
    configured_port: Any = None,
    configured_pid: Any = None,
    highlight: bool = True,
    dry_run: bool = False,
    tanyu_log_dir: str = "",
    allow_ui_fallback: bool = False,
) -> dict:
    """Send via 千牛 — openbot-first, no mouse circus.

    1) openbot CDP: openChat → verify buyer → insertText2Inputbox → UIA「发送」/Enter
       (content appears in input without cursor flying around)
    2) 探域 QN DLL (point-to-point, no mouse)
    3) Mouse paste ONLY if allow_ui_fallback=true (default false; not openbot-smooth)
    """
    buyer_id = str(buyer_id or "").strip()
    content = str(content or "").strip()
    account = str(account or "").strip()
    buyer_nick = str(buyer_nick or "").strip()
    if not buyer_id or not content or not account:
        return {
            "ok": False,
            "status": "blocked",
            "error": "buyer_id, account and content are required",
            "error_user": "缺少买家、旺旺账号或发送内容",
        }
    if content.replace("?", "").strip() == "" and set(content) <= {"?"}:
        return {
            "ok": False,
            "status": "blocked",
            "error": "content is only question marks",
            "error_user": "发送内容异常（只有 ???）：请重新输入中文后再发",
            "platform": "taobao",
            "real_send": False,
        }
    if dry_run:
        return {
            "ok": True,
            "status": "accepted",
            "via": "dry_run",
            "request_id": f"tb-dry-{int(time.time()*1000)}",
            "account": account,
            "buyer_id": buyer_id,
            "platform": "taobao",
            "error_user": "dry_run：仅模拟成功，未真实发出",
            "real_send": False,
        }

    request_id = f"tb-{int(time.time() * 1000)}-{secrets.token_hex(6)}"

    # Resolve wangwang nick early (openbot + DLL both need it)
    if not buyer_nick:
        buyer_nick = _qn_buyer_nick_for_im(buyer_id, "", log_dir=tanyu_log_dir)
    else:
        buyer_nick = _qn_buyer_nick_for_im(buyer_id, buyer_nick, log_dir=tanyu_log_dir)

    # ---- openbot path first ----
    try:
        ob = send_text_qianniu_openbot(buyer_id, content, buyer_nick=buyer_nick)
        if isinstance(ob, dict):
            ob.setdefault("request_id", request_id)
            ob.setdefault("account", account)
            ob.setdefault("buyer_id", buyer_id)
            ob.setdefault("platform", "taobao")
            ob.setdefault("buyer_nick", buyer_nick)
            # Success if input cleared after send click, or insert+click ok
            if ob.get("ok") and (ob.get("real_send") or ob.get("sent_click")):
                return ob
            # If text is in input but send button missing, still return that
            # result (actionable error_user about 无障碍) instead of silent UI paste
            if ob.get("inserted") and not ob.get("ok"):
                return ob
            openbot_fail = ob
        else:
            openbot_fail = {"error": "openbot path returned non-dict"}
    except Exception as exc:
        openbot_fail = {"error": f"openbot exception: {exc}"}

    login = read_qn_login_status(tanyu_log_dir)
    acc_login = None
    for row in login.get("accounts") or []:
        if str(row.get("account")) == account:
            acc_login = row
            break

    dll_port, pid, state = discover_qn_ports(
        force=True,
        configured_port=configured_port,
        configured_pid=configured_pid,
    )

    def _fail_no_mouse(*, reason: str, port: Any = None, tried_v: Any = None, last_log_v: str = "") -> dict:
        """Clean failure without moving the mouse (openbot-smooth policy)."""
        ob_hint = str(
            (openbot_fail or {}).get("error_user")
            or (openbot_fail or {}).get("error")
            or ""
        )
        return {
            "ok": False,
            "status": "failed",
            "error": reason,
            "error_user": (
                f"{reason}。"
                "当前按 openbot 方式发送需要：探域启动台打开千牛 + CDP 调试端口 + 无障碍模式。"
                "已禁用「鼠标乱点粘贴」兜底（不丝滑且易串台）。"
                + (f" 详情：{ob_hint}" if ob_hint else "")
            ),
            "via": "openbot_required",
            "real_send": False,
            "platform": "taobao",
            "buyer_nick": buyer_nick,
            "openbot": openbot_fail,
            "port_discovery": state,
            "port": port,
            "tried": tried_v,
            "log_result": last_log_v,
            "request_id": request_id,
        }

    if not dll_port:
        if allow_ui_fallback and buyer_nick:
            gate_ok, gate_detail = _ui_fallback_session_gate(
                buyer_nick, buyer_id=buyer_id, account=account, log_dir=tanyu_log_dir
            )
            if gate_ok:
                ok_ui, detail = _ui_send_qianniu(content)
                return {
                    "ok": False,
                    "status": "failed",
                    "via": "ui_paste",
                    "real_send": False,
                    "error": f"ui_paste tried={ok_ui}",
                    "error_user": "已用最小粘贴兜底（请确认气泡）。建议恢复 CDP 走 openbot。",
                    "ui": detail,
                    "gate": gate_detail,
                    "openbot": openbot_fail,
                    "request_id": request_id,
                    "platform": "taobao",
                }
        return _fail_no_mouse(reason="无 CDP（openbot）且无探域 DLL 端口")

    # request_id already allocated above (openbot path)
    ccode, short_id = _qn_split_buyer(buyer_id)
    marker = content[:40]
    dll_logged_in = bool(acc_login and acc_login.get("if_login")) or bool(login.get("any_logged_in"))

    # 1) Always focus conversation first (QnMsg 切换客户 is known-good)
    try:
        _qn_open_buyer(
            dll_port,
            pid or 0,
            account=account,
            buyer_id=buyer_id,
            buyer_nick=buyer_nick,
            platform_version=platform_version,
        )
        time.sleep(0.55)
    except Exception:
        pass

    last_log = "SKIPPED"
    tried: list[str] = []

    # 2) DLL path: prefer ifLogin=true; still try once when false (status can be stale)
    if True:
        im_candidates = []
        if short_id:
            im_candidates.append({"userId": short_id, "nick": buyer_nick or "", "appKey": "cntaobao"})
            if buyer_nick:
                im_candidates.append(
                    {
                        "userId": short_id,
                        "nick": buyer_nick,
                        "appKey": "cntaobao",
                        "targetId": short_id,
                        "display": buyer_nick,
                        "targetType": "3",
                    }
                )
        im_candidates.append(
            {"userId": ccode or buyer_id, "nick": buyer_nick or "", "appKey": "cntaobao", "targetId": short_id or ""}
        )
        if buyer_nick:
            im_candidates.append({"userId": buyer_nick, "nick": buyer_nick, "appKey": "cntaobao"})
            im_candidates.append(
                {"userId": f"cntaobao{buyer_nick}", "nick": f"cntaobao{buyer_nick}", "appKey": "cntaobao"}
            )
        if not dll_logged_in:
            tried.append("dll_try_despite_ifLogin=false")
            # fewer candidates when login flag false
            im_candidates = im_candidates[:2]

        last_log = "UNKNOWN"
        for im in im_candidates:
            payload = {
                "cmd": "send_text",
                "platformType": 0,
                "platformVersion": platform_version,
                "processId": pid or 0,
                "account": account,
                "reqId": request_id,
                "data": {
                    "account": account,
                    "SellerNick": account,
                    "BuyerCid": ccode or buyer_id,
                    "Context": content,
                    "ccode": ccode or buyer_id,
                    "buyerNick": buyer_nick,
                    "buyerId": short_id or buyer_id,
                    "imServerUser": im,
                    "isClearSecond": True,
                    "isHighLight": bool(highlight),
                    "msgList": [{"type": 0, "value": content, "coverImg": "", "platformMsgType": ""}],
                    "text": content,
                    "content": content,
                    "msg": content,
                    "reqId": request_id,
                    "productId": "",
                },
            }
            try:
                _qn_post(dll_port, payload)
            except Exception as exc:
                tried.append(f"post_err:{exc}")
                continue
            time.sleep(0.45)
            log_text = _qn_tail_log(tanyu_log_dir)
            last_log = _qn_parse_send_result(log_text, marker)
            tried.append(f"{im.get('userId')}:{last_log}")
            if last_log == "SUCCESS":
                return {
                    "ok": True,
                    "status": "accepted",
                    "via": "qn_dll",
                    "port": dll_port,
                    "pid": pid,
                    "request_id": request_id,
                    "account": account,
                    "buyer_id": buyer_id,
                    "platform": "taobao",
                    "real_send": True,
                    "log_result": last_log,
                    "tried": tried,
                }
    # 3) No mouse circus. Optional minimal UI only when explicitly enabled.
    if allow_ui_fallback and buyer_nick:
        gate_ok, gate_detail = _ui_fallback_session_gate(
            buyer_nick, buyer_id=buyer_id, account=account, log_dir=tanyu_log_dir
        )
        if gate_ok:
            ok_ui, detail = _ui_send_qianniu(content)
            return {
                "ok": False,
                "status": "failed",
                "via": "ui_paste",
                "real_send": False,
                "port": dll_port,
                "request_id": request_id,
                "platform": "taobao",
                "error": f"DLL={last_log}; ui_paste tried={ok_ui}",
                "error_user": f"DLL 未确认（{last_log}）；已用最小粘贴兜底，请看气泡。建议恢复 CDP。",
                "ui": detail,
                "gate": gate_detail,
                "tried": tried,
                "openbot": openbot_fail,
            }
    return _fail_no_mouse(
        reason=f"DLL未确认发出({last_log})且未走鼠标粘贴",
        port=dll_port,
        tried_v=tried,
        last_log_v=last_log,
    )


def _normalize_pdd_account(value: Any) -> str:
    account = str(value or "").strip()
    match = re.fullmatch(r"cs_(\d+)[_:](\d+)", account)
    if match:
        return f"cs_{match.group(1)}:{match.group(2)}"
    return account


def _pdd_log_files(log_dir: str = "") -> list[Any]:
    from pathlib import Path

    roots = []
    if log_dir:
        roots.append(Path(log_dir))
    default_root = Path(r"D:\kefuAgent\探域\tanyu2.9.1\logs")
    if default_root not in roots:
        roots.append(default_root)
    files = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("inside_*.log", "Injector_cnpdd*.log"):
            try:
                files.extend(path for path in root.glob(pattern) if path.is_file())
            except OSError:
                continue
    unique = {str(path.resolve()).lower(): path for path in files}
    return sorted(unique.values(), key=lambda path: path.stat().st_mtime, reverse=True)[:8]


def _pdd_log_cursor(log_dir: str = "") -> dict[str, int]:
    cursor: dict[str, int] = {}
    for path in _pdd_log_files(log_dir):
        try:
            cursor[str(path.resolve()).lower()] = path.stat().st_size
        except OSError:
            continue
    return cursor


def _pdd_log_delta(log_dir: str, cursor: dict[str, int], *, max_bytes: int = 500_000) -> str:
    chunks = []
    for path in _pdd_log_files(log_dir):
        try:
            key = str(path.resolve()).lower()
            size = path.stat().st_size
            start = min(int(cursor.get(key, 0)), size)
            if size - start > max_bytes:
                start = size - max_bytes
            if size <= start:
                continue
            with path.open("rb") as handle:
                handle.seek(start)
                chunks.append(handle.read(max_bytes).decode("utf-8", "replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _pdd_parse_open_confirmation(log_text: str, *, account: str, buyer_id: str) -> dict:
    target_account = _normalize_pdd_account(account)
    target_buyer = str(buyer_id or "").strip()
    seen = []
    native_pattern = re.compile(
        r"handleMsg_openCustomerDialog\]\s+open customer dialog,\s*"
        r"csId:\s*([^\s,]+)\s*,\s*userId:\s*([^\s\r\n]+)"
    )
    for match in native_pattern.finditer(log_text or ""):
        seller = _normalize_pdd_account(match.group(1))
        buyer = match.group(2).strip()
        seen.append({"source": "native_open", "account": seller, "buyer_id": buyer})
        if seller == target_account and buyer == target_buyer:
            return {
                "verified": True,
                "source": "native_open",
                "account": seller,
                "buyer_id": buyer,
            }

    current_pattern = re.compile(
        r'"cmd"\s*:\s*"currentBuyerChange".*?'
        r'"sellerId"\s*:\s*"([^"]*)".*?'
        r'"buyerId"\s*:\s*"([^"]*)"'
    )
    for line in (log_text or "").splitlines():
        match = current_pattern.search(line)
        if not match:
            continue
        seller = _normalize_pdd_account(match.group(1))
        buyer = match.group(2).strip()
        seen.append({"source": "current_buyer", "account": seller, "buyer_id": buyer})
        if seller == target_account and buyer == target_buyer:
            return {
                "verified": True,
                "source": "current_buyer",
                "account": seller,
                "buyer_id": buyer,
            }
    return {"verified": False, "seen": seen[-4:]}


def _pdd_wait_for_open_confirmation(
    log_dir: str,
    *,
    account: str,
    buyer_id: str,
    cursor: dict[str, int],
    timeout: float = 4.0,
) -> dict:
    deadline = time.monotonic() + max(0.0, float(timeout))
    last = {"verified": False, "seen": []}
    while True:
        last = _pdd_parse_open_confirmation(
            _pdd_log_delta(log_dir, cursor),
            account=account,
            buyer_id=buyer_id,
        )
        if last.get("verified") or time.monotonic() >= deadline:
            return last
        time.sleep(0.1)


_PDD_SEND_OK_MARKERS = ("Send_Seller_Msg_Success", "Send_Robot_Msg")
_PDD_SEND_MSG_RE = re.compile(
    r"utf8_msg:(.*?)(?:\s+(?:msg_type|text_or_picture|is_light_up|is_read_second|result):|$)"
)


def _pdd_parse_send_confirmation(
    log_text: str, *, account: str, buyer_id: str, content: str
) -> dict:
    """Check fresh Tanyu log delta for the plugin send-success receipt."""
    target_buyer = str(buyer_id or "").strip()
    want = re.sub(r"\s+", "", str(content or ""))[:48]
    seen = []
    for raw_line in (log_text or "").splitlines():
        if not any(marker in raw_line for marker in _PDD_SEND_OK_MARKERS):
            continue
        bid = re.search(r"buyer_id:(\d+)", raw_line)
        acc = re.search(r"cs_id:([^\s]+)", raw_line)
        msg = _PDD_SEND_MSG_RE.search(raw_line)
        got = re.sub(r"\s+", "", msg.group(1) if msg else "")[:48]
        seen.append(
            {
                "buyer_id": bid.group(1) if bid else "",
                "account": acc.group(1) if acc else "",
                "content_prefix": (msg.group(1) if msg else "")[:40],
            }
        )
        buyer_ok = bool(bid and bid.group(1) == target_buyer)
        content_ok = (not want) or (not got) or got == want or want.startswith(got) or got.startswith(want)
        if buyer_ok and content_ok:
            return {"verified": True, "account": acc.group(1) if acc else account, "buyer_id": target_buyer}
    return {"verified": False, "seen": seen[-4:]}


def _pdd_wait_for_send_confirmation(
    log_dir: str,
    *,
    account: str,
    buyer_id: str,
    content: str,
    cursor: dict[str, int],
    timeout: float = 4.0,
) -> dict:
    deadline = time.monotonic() + max(0.0, float(timeout))
    last = {"verified": False, "seen": []}
    while True:
        last = _pdd_parse_send_confirmation(
            _pdd_log_delta(log_dir, cursor),
            account=account,
            buyer_id=buyer_id,
            content=content,
        )
        if last.get("verified") or time.monotonic() >= deadline:
            return last
        time.sleep(0.1)


def open_chat_pdd(
    buyer_id: str,
    account: str,
    *,
    buyer_nick: str = "",
    platform_version: str = "3.5.0.40",
    configured_port: Any = None,
    configured_pid: Any = None,
    tanyu_log_dir: str = "",
) -> dict:
    """Open/focus a PDD conversation and verify the native dispatch in logs."""
    buyer_id = str(buyer_id or "").strip()
    account = _normalize_pdd_account(account)
    buyer_nick = str(buyer_nick or "").strip()
    if not buyer_id or not account:
        return {
            "ok": False,
            "status": "blocked",
            "error": "buyer_id and account required",
            "error_user": "缺少买家或客服账号，无法跳转拼多多会话",
            "real_send": False,
            "via": "open_chat_pdd",
        }

    dll_port, pid, state = discover_ports(
        force=True,
        configured_port=configured_port,
        configured_pid=configured_pid,
    )
    if not dll_port:
        return {
            "ok": False,
            "status": "failed",
            "error": "PddWorkbench DLL port unavailable",
            "port_discovery": state,
            "error_user": "未发现拼多多工作台/探域发送端口。请打开探域与拼多多商家工作台后再点跳转。",
            "real_send": False,
            "via": "open_chat_pdd",
            "manual": {
                "account": account,
                "buyer_id": buyer_id,
                "buyer_nick": buyer_nick,
            },
        }

    log_cursor = _pdd_log_cursor(tanyu_log_dir)
    request_id = f"jump-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    payload = {
        "cmd": "open_customer_dialog",
        "platformType": 1,
        "platformVersion": platform_version,
        "processId": pid or 0,
        "data": {
            "account": account,
            "imServerUser": {
                "userId": buyer_id,
                "nick": buyer_nick,
                "appKey": "cnpdd",
            },
            "isClearSecond": True,
            "isHighLight": True,
        },
        "reqId": request_id,
    }
    url = f"http://127.0.0.1:{dll_port}/tanyu/client/pdd/dll/httpTestApi"
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=4.0) as resp:
            raw = resp.read()
            err = _dll_response_error(int(resp.status), raw)
            if err:
                raise RuntimeError(err)
    except Exception as exc:
        _PORT_CACHE["expires"] = 0.0
        return {
            "ok": False,
            "status": "failed",
            "error": f"PDD open_customer_dialog failed: {exc}",
            "error_user": f"拼多多会话跳转请求失败：{exc}",
            "real_send": False,
            "via": "open_chat_pdd",
            "cmd": "open_customer_dialog",
            "request_id": request_id,
            "port": dll_port,
        }

    confirmation = _pdd_wait_for_open_confirmation(
        tanyu_log_dir,
        account=account,
        buyer_id=buyer_id,
        cursor=log_cursor,
    )
    if confirmation.get("verified"):
        return {
            "ok": True,
            "status": "accepted",
            "via": "open_chat_pdd",
            "cmd": "open_customer_dialog",
            "port": dll_port,
            "request_id": request_id,
            "real_send": False,
            "buyer_id": buyer_id,
            "account": account,
            "buyer_nick": buyer_nick,
            "verification": confirmation,
            "error_user": "",
        }

    return {
        "ok": False,
        "status": "failed",
        "error": "PDD workbench did not confirm the requested account and buyer",
        "error_user": (
            f"工作台已收到请求，但未确认跳到目标会话。请手动打开：账号 {account} / 买家 {buyer_nick or buyer_id}"
        ),
        "real_send": False,
        "via": "open_chat_pdd",
        "cmd": "open_customer_dialog",
        "request_id": request_id,
        "port": dll_port,
        "verification": confirmation,
        "manual": {
            "account": account,
            "buyer_id": buyer_id,
            "buyer_nick": buyer_nick,
        },
    }


def send_text(
    buyer_id: str,
    content: str,
    account: str,
    *,
    platform_version: str = "3.5.0.40",
    configured_port: Any = None,
    configured_pid: Any = None,
    highlight: bool = False,
    dry_run: bool = False,
    buyer_nick: str = "",
    tanyu_log_dir: str = "",
    is_expired=None,
) -> dict:
    buyer_id = str(buyer_id or "").strip()
    content = str(content or "").strip()
    account = str(account or "").strip()
    # normalize cs_xxx_yyy -> cs_xxx:yyy
    if account.startswith("cs_") and ":" not in account:
        parts = account.split("_")
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            account = f"cs_{parts[1]}:{parts[2]}"

    if not buyer_id or not content or not account:
        return {"ok": False, "status": "blocked", "error": "buyer_id, account and content are required"}

    if dry_run:
        return {
            "ok": True,
            "status": "accepted",
            "via": "dry_run",
            "request_id": f"dry-{int(time.time()*1000)}",
            "account": account,
            "buyer_id": buyer_id,
        }

    dll_port, pid, state = discover_ports(
        force=True,
        configured_port=configured_port,
        configured_pid=configured_pid,
    )
    if not dll_port:
        return {
            "ok": False,
            "status": "failed",
            "error": "PddWorkbench DLL port unavailable",
            "port_discovery": state,
            "error_user": "未发现探域工作台发送端口，请确认探域与拼多多工作台已打开",
        }

    request_id = f"bridge-{int(time.time() * 1000)}-{secrets.token_hex(6)}"
    # DLL silently ignores payloads with an empty imServerUser.nick (returns HTTP
    # 200 without delivering), so always pass the real buyer nick like open_chat_pdd.
    nick = str(buyer_nick or "").strip()
    log_cursor = _pdd_log_cursor(tanyu_log_dir)
    payload = {
        "cmd": "send_text",
        "platformType": 1,
        "platformVersion": platform_version,
        "processId": pid or 0,
        "data": {
            "account": account,
            "imServerUser": {"userId": buyer_id, "nick": nick, "appKey": "cnpdd"},
            "isClearSecond": True,
            "isHighLight": bool(highlight),
            "msgList": [{"type": 0, "value": content, "coverImg": "", "platformMsgType": ""}],
            "reqId": request_id,
            "productId": "",
        },
    }
    if callable(is_expired) and is_expired():
        return {"ok": False, "status": "expired", "real_send": False,
                "retryable": False, "via": "expired", "error": "automatic_command_expired"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"http://127.0.0.1:{dll_port}/tanyu/client/pdd/dll/httpTestApi"
    try:
        req = urlrequest.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            err = _dll_response_error(int(resp.status), raw)
            if err:
                raise RuntimeError(err)
        # HTTP 200 alone is NOT proof of delivery — the DLL is known to no-op
        # with 200. Confirm via the Tanyu send-success receipt before claiming sent.
        confirmation = _pdd_wait_for_send_confirmation(
            tanyu_log_dir,
            account=account,
            buyer_id=buyer_id,
            content=content,
            cursor=log_cursor,
            timeout=4.0,
        )
        if confirmation.get("verified"):
            return {
                "ok": True,
                "status": "confirmed",
                "real_send": True,
                "via": "dll+log",
                "port": dll_port,
                "request_id": request_id,
                "account": account,
                "buyer_id": buyer_id,
                "verification": confirmation,
            }
        return {
            "ok": True,
            "status": "indeterminate",
            "real_send": False,
            "via": "dll",
            "error": f"DLL accepted request but no send receipt within 4s (nick_empty={not nick})",
            "error_user": (
                f"已提交工作台但未在本地日志确认送达，请人工核对该会话（买家 {nick or buyer_id}）"
            ),
            "port": dll_port,
            "request_id": request_id,
            "account": account,
            "buyer_id": buyer_id,
            "verification": confirmation,
        }
    except Exception as exc:
        _PORT_CACHE["expires"] = 0.0
        return {
            "ok": False,
            "status": "failed",
            "error": f"DLL send failed: {exc}",
            "error_user": f"工作台发送失败：{exc}",
            "request_id": request_id,
        }


def channel_status(*, configured_port: Any = None, configured_pid: Any = None) -> dict:
    dll_port, pid, state = discover_ports(
        force=False,
        configured_port=configured_port,
        configured_pid=configured_pid,
    )
    return {
        "dll_port": dll_port,
        "workbench_pid": pid,
        "port_discovery": state,
        "dll_ready": bool(dll_port),
    }
