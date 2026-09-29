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
    # 探域 inside 日志的原生发送成功行（插件日志已停写，实际写的是这个）：
    #   [CMsgInterfaceWorkbench_pdd::handleMsg_sendMsg] send text message  success ,  userId = N message = <content>
    native = re.compile(
        r"handleMsg_sendMsg\]\s*send text message\s+success\s*,\s*userId\s*=\s*(\d+)\s*message\s*=\s*(.*)$"
    )
    for match in native.finditer(log_text or ""):
        bid = match.group(1).strip()
        raw = match.group(2).strip()
        got = re.sub(r"\s+", "", raw)[:48]
        seen.append({"buyer_id": bid, "account": account, "content_prefix": raw[:40]})
        buyer_ok = bool(bid) and bid == target_buyer
        content_ok = ((not want) or (not got) or got == want
                      or want.startswith(got) or got.startswith(want))
        if buyer_ok and content_ok:
            return {"verified": True, "account": account, "buyer_id": target_buyer,
                    "source": "inside"}
    return {"verified": False, "seen": seen[-4:]}


def _pdd_wait_for_send_confirmation(
    log_dir: str,
    *,
    account: str,
    buyer_id: str,
    content: str,
    cursor: dict[str, int],
    timeout: float = 10.0,
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
    confirm_timeout: float = 10.0,
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
            timeout=float(confirm_timeout),
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
            "error": f"DLL accepted request but no send receipt within {int(confirm_timeout)}s (nick_empty={not nick})",
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
