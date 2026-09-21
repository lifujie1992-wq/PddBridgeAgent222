# -*- coding: utf-8 -*-
"""Taobao / Qianniu channel: log receive + best-effort status/send.

Parse only real chat payloads (summary / originalData.text / jsview text).
Never promote protocol fields (qnVersion=1.0, reverse-qn, API type names) to messages.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import PlatformBase

TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

# Protocol / IPC junk that deep-scan used to promote into "buyer messages".
_NOISE_EXACT = {
    "1.0", "1.1.0", "0", "0.0", "true", "false", "null", "undefined",
    "hi", "连接成功", "connectSuccess",
    "reverse-qn", "receive_error", "receiveError",
    "onShopRobotReceriveNewMsgs", "recerivePeekNewMsgsRes", "receivePeekNewMsgsRes",
    "FindTargetMsg", "log-send-messageId", "workbench_msg", "js_msg",
    "bc_chat", "taobao", "cntaobao", "UnitComponent", "bubble",
    "pub", "xw", "ok", "success", "error",
}
_NOISE_PREFIXES = (
    "onShop", "recerive", "receive", "reverse-", "log-", "js_", "req_",
    "ws://", "wss://", "pic:", "http://", "https://",
)
# CamelCase / API identifiers: onFooBar, receiveXxxRes
_API_NAME_RE = re.compile(r"^[a-z]+[A-Z][A-Za-z0-9_]*$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
_PURE_ID_RE = re.compile(r"^[\d.]+$")
_LONG_TOKEN_RE = re.compile(r"^[\w\-#:.@]+$")


def _stable_hash(*parts: Any) -> str:
    import hashlib
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _canonical_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_ts(value: Any) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return int(ts)
    text = str(value).strip()
    m = TS_RE.search(text)
    if m:
        try:
            return int(time.mktime(time.strptime(m.group("ts")[:19], "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    try:
        return int(float(text))
    except Exception:
        return int(time.time())


def _decode_embedded_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return json.loads(json.loads(f'"{text}"'))
    except Exception:
        return None


def _hidden_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = create_no_window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _process_running(*names: str) -> List[dict]:
    """List running processes by name. Never use PowerShell (hangs on some machines)."""
    want = {n.lower().removesuffix(".exe") for n in names if n}
    if not want:
        return []
    found: List[dict] = []
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name", "pid"]):
            n = (p.info.get("name") or "").lower().removesuffix(".exe")
            if n in want:
                try:
                    found.append({"pid": int(p.info["pid"]), "name": n})
                except Exception:
                    continue
        return found
    except Exception:
        pass
    # tasklist fallback (fast, no WMI)
    for name in want:
        try:
            raw = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                **_hidden_kwargs(),
            )
            text = raw.decode("gbk", "replace")
            for line in text.splitlines():
                parts = [x.strip().strip('"') for x in line.split(",")]
                if len(parts) >= 2 and parts[1].isdigit():
                    found.append({"pid": int(parts[1]), "name": name})
        except Exception:
            continue
    return found


def is_protocol_noise_content(content: str) -> bool:
    """True if content is IPC/protocol junk, not human chat."""
    t = _canonical_text(content)
    if not t:
        return True
    if t in _NOISE_EXACT:
        return True
    if _VERSION_RE.fullmatch(t):
        return True
    if _PURE_ID_RE.fullmatch(t) and len(t) >= 6:
        # Pure long numbers are ids (buyer uid etc.), not chat text
        return True
    if _API_NAME_RE.fullmatch(t):
        return True
    low = t.lower()
    for pref in _NOISE_PREFIXES:
        if low.startswith(pref.lower()):
            return True
    # Protocol fields glued onto real words (e.g. "在 text_or_picture:0 is_light_up:true")
    protocol_markers = (
        "text_or_picture", "is_light_up", "is_read_second", "js_version",
        "qnversion", "forwardisopen", "req_action", "templateid", "messageid",
        "clientid", "sorttimemicrosecond", "paasappkey", "pushmsgtype",
        "originaldata", "msgextrainfo", "layoutstyle", "weexjs",
    )
    if any(m in low.replace(" ", "") or m in low for m in protocol_markers):
        return True
    # Login nicks / tokens mistaken as text
    if re.fullmatch(r"tb\d{6,}", t, flags=re.I):
        return True
    if re.fullmatch(r"cntaobao\S+", t, flags=re.I):
        return True
    # Path-like / hex blobs
    if t.startswith("D:\\") or t.startswith("C:\\") or "AliWorkbench" in t:
        return True
    if _LONG_TOKEN_RE.fullmatch(t) and len(t) > 40:
        return True
    if len(t) > 500:
        return True
    return False


def _nick_of(node: Any) -> str:
    if not isinstance(node, dict):
        return str(node or "").strip()
    return str(
        node.get("nick")
        or node.get("display")
        or node.get("targetId")
        or node.get("uid")
        or ""
    ).strip()


def _extract_text_from_msg_detail(detail: dict) -> str:
    """Prefer real chat text fields from a Qianniu message detail object."""
    if not isinstance(detail, dict):
        return ""
    # Primary: summary / originalData.text
    for key in ("summary", "text", "content", "msg"):
        val = _canonical_text(detail.get(key))
        if val and not is_protocol_noise_content(val):
            return val
    original = detail.get("originalData")
    if isinstance(original, str):
        original = _decode_embedded_json(original) or {}
    if isinstance(original, dict):
        for key in ("text", "content", "msg", "summary"):
            val = _canonical_text(original.get(key))
            if val and not is_protocol_noise_content(val):
                return val
        jsview = original.get("jsview")
        if isinstance(jsview, list):
            for item in jsview:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                if isinstance(value, dict):
                    val = _canonical_text(value.get("text") or value.get("content") or "")
                else:
                    val = _canonical_text(value)
                if val and not is_protocol_noise_content(val):
                    return val
    return ""


def _walk_message_details(obj: Any, out: List[dict], depth: int = 0) -> None:
    """Collect dicts that look like real Qianniu chat message nodes."""
    if depth > 8 or obj is None:
        return
    if isinstance(obj, dict):
        has_party = any(k in obj for k in ("fromid", "toid", "fromId", "toId", "cid"))
        has_body = any(k in obj for k in ("summary", "originalData", "mcode", "templateId"))
        if has_party and has_body:
            out.append(obj)
        # Nested double-encoded "data" string (common in PeekNewMsgs)
        for key in ("data", "result", "msgDetail", "message", "originData"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip().startswith(("{", "[")):
                decoded = _decode_embedded_json(val)
                if decoded is not None:
                    _walk_message_details(decoded, out, depth + 1)
            else:
                _walk_message_details(val, out, depth + 1)
        return
    if isinstance(obj, list):
        for item in obj[:50]:
            _walk_message_details(item, out, depth + 1)


class TaobaoPlatform(PlatformBase):
    name = "taobao"
    label = "淘宝/千牛"
    header_color = "#ff6a00"
    default_config_name = "bridge_config.taobao.json"
    exe_product_name = "QianniuBridgeAgent"

    def log_attach_specs(self) -> List[Tuple[str, str, bool]]:
        return [
            ("INJECT_TB", "Injector_cntaobao*.log", False),
            ("INSIDE_TB", "inside_*.log", False),
            ("QNMSG", "QnMsg*.log", False),
            ("SMART", "SmartRobot_*.log", False),
            ("INJECTTOOL", "InjectTool_*.log", False),
        ]

    def parse_line(self, line: str, source: str = "") -> List[Dict[str, Any]]:
        text = str(line or "")
        if not any(
            key in text
            for key in (
                "workbench_msg", "js_msg", "originData", "sellerNick", "shopName",
                "summary", "originalData", "fromid", "PeekNewMsgs", "FindTargetMsg",
                "messageId", "ccode", "templateId",
            )
        ):
            return []

        results: List[Dict[str, Any]] = []
        account_hint = ""
        m_acc = re.search(r'"account"\s*:\s*"([^"]+)"', text)
        if m_acc:
            account_hint = m_acc.group(1).strip()
        # Prefer seller nick from toid when account missing
        m_seller = re.search(r'"toid"\s*:\s*\{[^}]*"nick"\s*:\s*"([^"]+)"', text)
        seller_hint = m_seller.group(1).strip() if m_seller else ""

        # originData embedded blobs (pretty + compact)
        origin_blobs: List[str] = []
        for match in re.finditer(r'"originData"\s*:\s*"((?:\\.|[^"\\])*)"', text):
            origin_blobs.append(match.group(1))

        for blob in origin_blobs:
            obj = _decode_embedded_json(blob)
            if not isinstance(obj, dict):
                continue
            results.extend(
                self._parse_origin_obj(
                    obj, source=source, account=account_hint or seller_hint, line=text,
                )
            )

        # Direct chrome IPC line (no originData wrapper)
        if "fromid" in text and "summary" in text:
            try:
                # Find outermost JSON
                start = text.find("{")
                if start >= 0:
                    outer = json.loads(text[start:])
                    if isinstance(outer, dict):
                        results.extend(
                            self._parse_origin_obj(
                                outer, source=source, account=account_hint or seller_hint, line=text,
                            )
                        )
            except Exception:
                pass

        # Direct workbench_msg JSON line
        if text.strip().startswith("{") and ("workbench_msg" in text or "js_msg" in text):
            try:
                outer = json.loads(text[text.find("{") :])
            except Exception:
                outer = None
            if isinstance(outer, dict):
                data = outer.get("data") if isinstance(outer.get("data"), dict) else {}
                acc = str(data.get("account") or account_hint or seller_hint or "").strip()
                od = data.get("originData")
                if isinstance(od, str):
                    obj = _decode_embedded_json(od)
                    if isinstance(obj, dict):
                        results.extend(self._parse_origin_obj(obj, source=source, account=acc, line=text))
                elif isinstance(data, dict):
                    results.extend(self._parse_origin_obj(data, source=source, account=acc, line=text))

        unique: Dict[str, Dict[str, Any]] = {}
        for msg in results:
            content = _canonical_text(msg.get("content"))
            if not content or is_protocol_noise_content(content):
                continue
            if not msg.get("buyer_id") or msg.get("buyer_id") == "unknown":
                # Require identifiable buyer for chat rows
                continue
            key = _stable_hash(
                msg.get("account"), msg.get("buyer_id"), msg.get("msg_id"),
                msg.get("role"), content,
            )
            msg["content"] = content
            msg["idempotency_key"] = key[:32]
            msg["platform"] = self.name
            unique[key] = msg
        return list(unique.values())

    def _parse_origin_obj(
        self, obj: dict, *, source: str, account: str, line: str,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        ts_line = _normalize_ts(TS_RE.search(line).group("ts") if TS_RE.search(line) else time.time())

        cmd = str(obj.get("cmd") or "")
        if cmd in {"connectSuccess"}:
            return out

        details: List[dict] = []
        _walk_message_details(obj, details)

        # Fallback: single shallow message-like object
        if not details and isinstance(obj, dict):
            content_try = _extract_text_from_msg_detail(obj)
            if content_try:
                details = [obj]

        seller_account = str(account or "").strip()
        for detail in details:
            content = _extract_text_from_msg_detail(detail)
            if not content or is_protocol_noise_content(content):
                continue

            fromid = detail.get("fromid") or detail.get("fromId") or {}
            toid = detail.get("toid") or detail.get("toId") or {}
            from_nick = _nick_of(fromid)
            to_nick = _nick_of(toid)

            cid = detail.get("cid") if isinstance(detail.get("cid"), dict) else {}
            ccode = str(
                cid.get("ccode")
                or detail.get("ccode")
                or ""
            ).strip()

            # Buyer is the non-seller party. Seller nick is shop account (sbpgklso).
            buyer = ""
            role = "user"
            if seller_account:
                if from_nick and from_nick == seller_account:
                    role = "mall_cs"
                    buyer = to_nick or ccode
                elif to_nick and to_nick == seller_account:
                    role = "user"
                    buyer = from_nick or ccode
                else:
                    # Prefer fromid as buyer for inbound chat
                    buyer = from_nick or ccode or to_nick
            else:
                # Infer seller as toid when fromid looks like buyer (tb* / long id)
                if from_nick and to_nick:
                    buyer = from_nick
                    seller_account = to_nick
                    role = "user"
                else:
                    buyer = from_nick or ccode or to_nick

            if not buyer:
                continue

            # Prefer stable ccode as buyer_id when available (conversation key)
            buyer_id = ccode or buyer

            msg_id = ""
            mcode = detail.get("mcode") if isinstance(detail.get("mcode"), dict) else {}
            if mcode:
                msg_id = str(mcode.get("messageId") or mcode.get("clientId") or "").strip()
            if not msg_id:
                msg_id = str(
                    detail.get("messageId")
                    or detail.get("msg_id")
                    or detail.get("clientId")
                    or ""
                ).strip()
            send_ts = _normalize_ts(
                detail.get("sendTime")
                or detail.get("sortTimeMicrosecond")
                or detail.get("ts")
                or ts_line
            )
            if not msg_id:
                msg_id = f"tb-{_stable_hash(seller_account, buyer_id, content, send_ts)[:16]}"

            out.append({
                "msg_id": msg_id,
                "buyer_id": buyer_id,
                "role": role,
                "content": content,
                "ts": send_ts,
                "account": seller_account,
                "source": source or "taobao",
                "delivery_status": "",
                "platform": self.name,
                "raw_cmd": cmd or str(obj.get("req_action") or ""),
                "buyer_nick": from_nick if role == "user" else to_nick,
            })
        return out

    def channel_status(self, cfg: dict) -> Dict[str, Any]:
        # Keep this path under ~1s: process list only + cached ports. No PowerShell.
        wb = _process_running("AliWorkbench")
        inj = _process_running("Injector_taobao")
        from ..channel import discover_qn_ports, read_qn_login_status, _discover_qn_cdp_port
        qn_port, qn_pid, qn_state = None, None, "skipped"
        cdp_port = None
        try:
            qn_port, qn_pid, qn_state = discover_qn_ports(
                force=False,
                configured_port=cfg.get("dll_port"),
                configured_pid=cfg.get("workbench_pid"),
            )
        except Exception as exc:
            qn_state = f"err:{exc}"
        try:
            cdp_port = _discover_qn_cdp_port(force=False)
        except Exception:
            cdp_port = None
        try:
            login = read_qn_login_status(str(cfg.get("tanyu_log_dir") or ""))
        except Exception:
            login = {"ok": False, "any_logged_in": False}
        logged = bool(login.get("any_logged_in"))
        parts = []
        if qn_port:
            parts.append(f"探域DLL:{qn_port}")
        if cdp_port:
            parts.append(f"DevTools:{cdp_port}")
        if cfg.get("dry_run"):
            parts.append("dry_run开着不会真发")
        if login.get("ok") and not logged:
            parts.append("ifLogin=false时DLL易失败")
        # openbot inject WS (no 探域) — primary send path for taobao
        ob_connected = False
        inj_any = False
        inj_all = False
        try:
            from ..openbot_ws import start_openbot_bridge, status_snapshot
            from ..qn_inject import inject_status

            start_openbot_bridge()
            snap = status_snapshot()
            ob_connected = bool(snap.get("openbot_connected"))
            inj_st = inject_status()
            inj_any = bool(inj_st.get("injected"))
            inj_all = bool(inj_st.get("all_injected"))
            if ob_connected:
                parts.append("openbot桥:已连接")
            elif inj_all:
                parts.append("openbot桥:已注入未连接(请开聊天页)")
            elif inj_any:
                parts.append("openbot桥:仅部分版本已注入(请管理员重开助手)")
            else:
                parts.append("openbot桥:未注入")
        except Exception:
            parts.append("openbot桥:err")
        if not parts:
            parts.append("通道探测中")

        can_send = bool(wb) and (bool(qn_port) or bool(cdp_port) or ob_connected)
        soft_ready = bool(wb)
        if can_send:
            state = "ready"
        elif wb and inj_all and not ob_connected:
            state = "openbot_waiting_chat"
        elif wb and inj_any and not ob_connected:
            state = "openbot_partial_injection"
        elif wb and not (qn_port or cdp_port or inj_any):
            state = "port_missing"
        elif wb:
            state = "workbench_only"
        elif inj:
            state = "injector_only"
        else:
            state = "not_found"

        if can_send:
            if ob_connected:
                hint = "openbot 桥已连接 · 可发送（无需探域） · " + " · ".join(parts)
            else:
                hint = "千牛可发送 · " + " · ".join(parts)
            if not inj:
                hint += " · 未检出 Injector_taobao（收消息可能弱）"
        elif soft_ready:
            hint = (
                "已检测到千牛进程。"
                + (
                    " openbot 脚本已全部注入，请打开接待台「聊天窗口」等待连上 Agent；"
                    if inj_all
                    else " 仅部分千牛版本完成注入，请以管理员身份重开 Agent；"
                    if inj_any
                    else " 请用管理员运行 Agent 完成 openbot 注入并重启千牛；"
                )
                + "并开启无障碍模式。"
            )
        else:
            hint = "请打开千牛接待台；发送走 openbot 注入桥（imsdk 写字+无障碍点发送，无需探域）"
        # GUI "can send" green only when we have a real send channel
        # soft_ready keeps banner from saying "请打开拼多多"
        return {
            # GUI uses dll_ready as "can send" switch for both platforms.
            # soft_ready (workbench up) => treat as ready for banner so user can try send;
            # actual send will report precise CDP/无障碍 errors.
            "dll_ready": bool(can_send or soft_ready),
            "dll_port": qn_port,
            "cdp_port": cdp_port,
            "workbench_pid": qn_pid or (wb[0]["pid"] if wb else None),
            "injector_pid": inj[0]["pid"] if inj else None,
            "port_discovery": f"{state}:{qn_state}",
            "platform": self.name,
            "workbench_count": len(wb),
            "injector_count": len(inj),
            "dry_run": bool(cfg.get("dry_run")),
            "qn_login": login,
            "hint": hint,
            "openbot_connected": ob_connected,
            "openbot_injected": inj_any,
            "openbot_all_injected": inj_all,
            "receive_ready": bool(ob_connected or inj),
        }

    def open_chat(
        self,
        buyer_id: str,
        account: str,
        *,
        buyer_nick: str = "",
        cfg: dict | None = None,
    ) -> dict:
        """Jump to 千牛 chat via application.openChat (no message send)."""
        buyer_id = str(buyer_id or "").strip()
        account = str(account or "").strip()
        nick = str(buyer_nick or "").strip()
        cfg = cfg or {}
        try:
            from .. import channel as ch
            if hasattr(ch, "open_chat_qianniu"):
                return ch.open_chat_qianniu(buyer_id, account, buyer_nick=nick, cfg=cfg)
            from ..openbot_ws import invoke_imsdk
            target_nick = nick or buyer_id
            if not target_nick:
                return {
                    "ok": False,
                    "status": "blocked",
                    "error": "missing nick",
                    "error_user": "缺少买家旺旺昵称，无法跳转",
                    "real_send": False,
                    "via": "open_chat",
                }
            # Strip cntaobao prefix if already present
            pure = target_nick[8:] if target_nick.startswith("cntaobao") else target_nick
            res = invoke_imsdk("application.openChat", {"nick": f"cntaobao{pure}"}, timeout=8.0)
            ok = res is not None
            # invoke may return nested error payloads
            if isinstance(res, dict) and (res.get("error") or res.get("success") is False):
                ok = bool(res.get("success") or res.get("ok"))
            return {
                "ok": ok,
                "status": "opened" if ok else "failed",
                "result": res if isinstance(res, (dict, list, str, int, float, bool, type(None))) else str(res)[:500],
                "error_user": "" if ok else "千牛 openChat 未确认成功，请手动点开会话",
                "real_send": False,
                "via": "open_chat",
                "buyer_id": buyer_id,
                "account": account,
                "buyer_nick": pure,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "error": str(exc),
                "error_user": f"跳转千牛会话失败：{exc}",
                "real_send": False,
                "via": "open_chat",
            }

    def send_text(
        self,
        buyer_id: str,
        content: str,
        account: str,
        *,
        cfg: dict,
        dry_run: bool = False,
    ) -> dict:
        buyer_id = str(buyer_id or "").strip()
        content = _canonical_text(content)
        account = str(account or "").strip()
        if not buyer_id or not content or not account:
            return {
                "ok": False,
                "status": "blocked",
                "error": "buyer_id, account and content are required",
                "error_user": "缺少买家、旺旺账号或发送内容",
            }
        if is_protocol_noise_content(content):
            return {
                "ok": False,
                "status": "blocked",
                "error": "content looks like protocol noise",
                "error_user": "发送内容疑似协议噪声，已拦截",
            }
        use_dry = bool(dry_run or cfg.get("dry_run"))
        buyer_nick = str(cfg.get("_buyer_nick") or "").strip()
        try:
            from ..channel import send_text_qianniu, read_qn_login_status
            result = send_text_qianniu(
                buyer_id,
                content,
                account,
                buyer_nick=buyer_nick,
                platform_version=str(cfg.get("platform_version") or "9.77.01N"),
                configured_port=cfg.get("dll_port"),
                configured_pid=cfg.get("workbench_pid"),
                highlight=True,
                dry_run=use_dry,
                tanyu_log_dir=str(cfg.get("tanyu_log_dir") or ""),
                allow_ui_fallback=bool(cfg.get("allow_ui_fallback", False)),
            )
            # attach login snapshot for GUI diagnostics
            if isinstance(result, dict) and "login" not in result:
                result["login"] = read_qn_login_status(str(cfg.get("tanyu_log_dir") or ""))
            return result
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "error_user": f"千牛发送失败：{exc}",
                "platform": self.name,
                "real_send": False,
            }
