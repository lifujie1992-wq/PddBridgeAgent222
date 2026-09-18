# -*- coding: utf-8 -*-
"""Lightweight Tanyu log line parser for bridge event upload.

Not a full copy of app.parse_line — covers the high-volume buyer/cs paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List


TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
ACCOUNT_RE = re.compile(r"cs[_\-]?(?P<mall>\d+)[:_\-](?P<uid>\d+)", re.I)
PLAIN_ACCOUNT_RE = re.compile(r"^(?P<mall>\d+)[:_\-](?P<uid>\d+)$")
GOODS_ID_RE = re.compile(
    r"(?:goods[_-]?id|goodsId|goodsID)\s*[=:]\s*(\d{6,20})",
    re.I,
)
GOODS_URL_RE = re.compile(
    r"(?:yangkeduo|pinduoduo)\.com/[^\s\"'<>]*?[?&]goods_id=(\d{6,20})",
    re.I,
)
LINE_MARKERS = (
    "chat_message",
    "buyer_msg",
    "dllRecvCallBack",
    "dllSendCallBack",
    "Send_Robot_Msg",
    "Send_Seller_Msg",
    "business_message",
    "接收聊天消息",
    "消息内容",
    "鎺ユ敹鑱婂ぉ娑堟伅",
    "娑堟伅鍐呭",
    "originData",
    "拼多多-ImWs-上报聊天消息成功",
    "firstLineOrderList",
    "orderInfoListJson",
)
LOGRUS_MARKERS = ("接收聊天消息", "消息内容:[", "鎺ユ敹鑱婂ぉ娑堟伅", "娑堟伅鍐呭:[")

PARSER_PROFILE_SCHEMA_VERSION = 1
_PROFILE_FIELDS = {
    "source_ref",
    "buyer_id",
    "content",
    "message_id",
    "request_id",
    "create_time",
    "message_time",
    "buyer_nick",
    "shop_name",
    "account",
    "order_context",
}
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
BUILTIN_PARSER_PROFILE: Dict[str, Any] = {
    "schema_version": PARSER_PROFILE_SCHEMA_VERSION,
    "version": "pdd-imws-v2-business-message",
    "candidate_markers": list(LINE_MARKERS),
    "json_fields": {
        "source_ref": ["body.sourceRef"],
        "buyer_id": [
            "body.buyerAccount",
            "body.buyer_id",
            "body.buyerId",
            "body.userId",
        ],
        "content": ["body.content.value"],
        "message_id": ["body.msgInfo.messageId"],
        "request_id": ["reqId"],
        "create_time": ["createTime"],
        "message_time": ["body.msgTime"],
        "buyer_nick": ["body.buyerNick"],
        "shop_name": ["body.mallName", "body.shopName", "body.mall_name"],
        "account": ["body.cs_id", "body.account", "cs_id", "account"],
        "order_context": ["body.firstLineOrderList", "body.orderInfoListJson"],
    },
    "role_map": {"BUYER": "user", "SELLER": "mall_cs", "MALL_CS": "mall_cs"},
}
_PROFILE_LOCK = threading.RLock()
_ACTIVE_PROFILE: Dict[str, Any] = json.loads(json.dumps(BUILTIN_PARSER_PROFILE))
_PROFILE_STATUS: Dict[str, Any] = {
    "version": BUILTIN_PARSER_PROFILE["version"],
    "source": "builtin",
    "last_error": "",
}


def _stable_hash(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _validate_parser_profile(profile: Any) -> Dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("parser_profile must be a JSON object")
    allowed = {"schema_version", "version", "candidate_markers", "json_fields", "role_map"}
    unknown = sorted(set(profile) - allowed)
    if unknown:
        raise ValueError(f"unsupported parser_profile keys: {', '.join(unknown)}")
    if int(profile.get("schema_version") or 0) != PARSER_PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported parser_profile schema_version")
    version = str(profile.get("version") or "").strip()
    if not version or len(version) > 80:
        raise ValueError("parser_profile version is required")
    if version == "pdd-imws-v1":
        raise ValueError("legacy parser_profile lacks business_message support")

    markers = profile.get("candidate_markers")
    if not isinstance(markers, list) or not markers or len(markers) > 128:
        raise ValueError("candidate_markers must contain 1..128 strings")
    clean_markers: List[str] = []
    for marker in markers:
        value = str(marker or "").strip()
        if not value or len(value) > 160:
            raise ValueError("invalid candidate marker")
        if value not in clean_markers:
            clean_markers.append(value)

    raw_fields = profile.get("json_fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("json_fields must be an object")
    unknown_fields = sorted(set(raw_fields) - _PROFILE_FIELDS)
    if unknown_fields:
        raise ValueError(f"unsupported parser_profile fields: {', '.join(unknown_fields)}")
    missing_fields = sorted({"source_ref", "buyer_id", "content"} - set(raw_fields))
    if missing_fields:
        raise ValueError(f"parser_profile is missing fields: {', '.join(missing_fields)}")
    fields: Dict[str, List[str]] = {}
    for field, raw_paths in raw_fields.items():
        paths = [raw_paths] if isinstance(raw_paths, str) else raw_paths
        if not isinstance(paths, list) or not paths or len(paths) > 16:
            raise ValueError(f"json_fields.{field} must contain 1..16 paths")
        clean_paths: List[str] = []
        for raw_path in paths:
            path = str(raw_path or "").strip()
            if not _PATH_RE.fullmatch(path) or len(path) > 160:
                raise ValueError(f"invalid JSON field path: {path!r}")
            if path not in clean_paths:
                clean_paths.append(path)
        fields[str(field)] = clean_paths

    raw_roles = profile.get("role_map")
    if not isinstance(raw_roles, dict) or not raw_roles or len(raw_roles) > 16:
        raise ValueError("role_map must contain 1..16 entries")
    roles: Dict[str, str] = {}
    for raw_source, raw_role in raw_roles.items():
        source = str(raw_source or "").strip().upper()
        role = str(raw_role or "").strip().lower()
        if not source or len(source) > 40 or role not in {"user", "mall_cs"}:
            raise ValueError("role_map only supports user and mall_cs")
        roles[source] = role
    if roles.get("BUYER") != "user":
        raise ValueError("role_map.BUYER must map to user")
    return {
        "schema_version": PARSER_PROFILE_SCHEMA_VERSION,
        "version": version,
        "candidate_markers": clean_markers,
        "json_fields": fields,
        "role_map": roles,
    }


def configure_parser_profile(profile: Any = None, cache_path: str | Path | None = None) -> bool:
    """Activate a data-only parser profile, falling back to the last good profile."""
    global _ACTIVE_PROFILE, _PROFILE_STATUS
    cache = Path(cache_path) if cache_path else None
    errors: List[str] = []
    candidates: List[tuple[str, Any]] = []
    if profile not in (None, ""):
        if isinstance(profile, (str, Path)):
            try:
                profile = json.loads(Path(profile).read_text(encoding="utf-8-sig"))
            except Exception as exc:
                errors.append(f"configured profile: {exc}")
                profile = None
        if profile is not None:
            candidates.append(("configured", profile))
    if cache and cache.is_file():
        try:
            candidates.append(("cache", json.loads(cache.read_text(encoding="utf-8-sig"))))
        except Exception as exc:
            errors.append(f"profile cache: {exc}")
    candidates.append(("builtin", BUILTIN_PARSER_PROFILE))

    selected: Dict[str, Any] | None = None
    selected_source = "builtin"
    for source, candidate in candidates:
        try:
            selected = _validate_parser_profile(candidate)
            selected_source = source
            break
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    if selected is None:
        selected = json.loads(json.dumps(BUILTIN_PARSER_PROFILE))
    with _PROFILE_LOCK:
        _ACTIVE_PROFILE = selected
        _PROFILE_STATUS = {
            "version": selected["version"],
            "source": selected_source,
            "last_error": "; ".join(errors)[:500],
        }
    if selected_source == "configured" and cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(cache.suffix + ".tmp")
            temporary.write_text(
                json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(cache)
        except OSError as exc:
            with _PROFILE_LOCK:
                _PROFILE_STATUS["last_error"] = f"profile cache write: {exc}"[:500]
    return selected_source in {"configured", "cache"}


def parser_profile_status() -> Dict[str, Any]:
    with _PROFILE_LOCK:
        return dict(_PROFILE_STATUS)


def _profile_snapshot() -> Dict[str, Any]:
    with _PROFILE_LOCK:
        return _ACTIVE_PROFILE


def _canonical_account(value: Any) -> str:
    if isinstance(value, dict):
        mall = str(value.get("mall") or value.get("mallId") or value.get("mall_id") or "").strip()
        uid = str(value.get("uid") or value.get("userId") or value.get("user_id") or "").strip()
        if mall.isdigit() and uid.isdigit():
            return f"cs_{mall}:{uid}"
        value = value.get("cs_id") or value.get("account") or ""
    text = str(value or "").strip()
    m = ACCOUNT_RE.search(text)
    if m:
        return f"cs_{m.group('mall')}:{m.group('uid')}"
    m = PLAIN_ACCOUNT_RE.fullmatch(text)
    if m:
        return f"cs_{m.group('mall')}:{m.group('uid')}"
    text = text.replace("cs-", "cs_")
    if text.startswith("cs_") and "_" in text[3:] and ":" not in text:
        mall, _, rest = text[3:].partition("_")
        if mall.isdigit() and rest.isdigit():
            return f"cs_{mall}:{rest}"
    return text


def _canonical_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_pdd_system_message(value: dict) -> bool:
    """Identify PDD control notices that are not customer messages."""
    if not isinstance(value, dict):
        return False
    template = str(value.get("template_name") or value.get("template") or "").strip().lower()
    if template == "mall_robot_man_intervention_and_restart":
        return True
    try:
        message_type = int(value.get("message_type", value.get("type", -1)))
    except (TypeError, ValueError):
        message_type = -1
    if message_type == 31 and bool(value.get("no_unreply_hint")) and bool(value.get("conv_silent")):
        return True
    content = _canonical_text(value.get("content"))
    return "机器人已暂停接待" in content and "立即恢复接待" in content


def _unescape(value: str) -> str:
    """Expand JSON-style escapes without corrupting UTF-8 Chinese.

    Never use ``bytes(...).decode('unicode_escape')`` on whole strings — that
    re-interprets multi-byte UTF-8 and produces classic 乱码 (å¥½ç… / Ã¤ÂºÂ²).
    """
    text = str(value or "")
    if not text:
        return ""
    if "\\" not in text:
        return text
    try:
        # Safe for fragments with \", \n, \uXXXX
        return json.loads(f'"{text}"')
    except Exception:
        return (
            text.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )


def _repair_mojibake(text: str) -> str:
    """Best-effort fix for UTF-8/GB18030 bytes decoded as Latin-1."""
    original = str(text or "")
    if not original or not any(ord(c) >= 0x80 for c in original):
        return original

    def score(value: str) -> int:
        cjk = sum(1 for c in value if "\u4e00" <= c <= "\u9fff")
        mojibake = sum(value.count(marker) for marker in ("Ã", "Â", "å", "ä", "æ"))
        controls = sum(1 for c in value if 0x80 <= ord(c) <= 0x9F)
        replacements = value.count("\ufffd")
        return cjk * 20 - mojibake * 3 - controls * 5 - replacements * 20

    candidates = [original]
    current = original
    for _ in range(3):
        try:
            repaired = current.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if not repaired or repaired == current:
            break
        candidates.append(repaired)
        current = repaired
    for candidate in tuple(candidates):
        try:
            repaired = candidate.encode("latin-1").decode("gb18030")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired and repaired != candidate:
            candidates.append(repaired)
    best = max(candidates, key=score)
    return best.replace("\ufffd", "").strip() or original


def _normalize_ts(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        ts = float(value)
        if not math.isfinite(ts):
            return 0
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
        ts = float(text)
        if not math.isfinite(ts):
            return 0
        if ts > 1e12:
            ts /= 1000.0
        return int(ts)
    except Exception:
        return 0


def _platform_time_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if numeric >= 100_000_000_000:
                return str(int(round(numeric)))
            if numeric >= 1_000_000_000:
                return str(int(round(numeric * 1000.0)))
            return format(numeric, ".6f").rstrip("0").rstrip(".")
        except Exception:
            return str(value)
    text = str(value).strip()
    try:
        return _platform_time_key(float(text))
    except (TypeError, ValueError):
        return text


def _extract_account(line: str) -> str:
    m = ACCOUNT_RE.search(line)
    return _canonical_account(m.group(0)) if m else ""


def is_candidate_line(line: str) -> bool:
    markers = _profile_snapshot().get("candidate_markers") or LINE_MARKERS
    return any(key in line for key in markers)


def _path_value(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _profile_value(event: dict, field: str) -> Any:
    paths = (_profile_snapshot().get("json_fields") or {}).get(field) or []
    for path in paths:
        value = _path_value(event, path)
        if value not in (None, ""):
            return value
    return None


def _json_values(text: str) -> List[Any]:
    """Decode complete or embedded JSON values without altering Unicode bytes."""
    raw = str(text or "").strip()
    if not raw:
        return []
    values: List[Any] = []
    fingerprints: set[str] = set()

    def add(value: Any) -> None:
        try:
            fingerprint = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            fingerprint = repr(value)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            values.append(value)

    for candidate in (raw, _unescape(raw)):
        try:
            add(json.loads(candidate))
        except Exception:
            pass
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", candidate):
            try:
                value, _end = decoder.raw_decode(candidate, match.start())
            except (json.JSONDecodeError, ValueError):
                continue
            add(value)
    return values


def _content_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _canonical_text(_repair_mojibake(_unescape(str(value or ""))))


def _json_string_field(text: str, field: str) -> List[Any]:
    """Decode a JSON value after a named field, even without outer braces."""
    values: List[Any] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(rf'["\']{re.escape(field)}["\']\s*:\s*', text, re.I):
        try:
            value, _end = decoder.raw_decode(text, match.end())
        except (json.JSONDecodeError, ValueError):
            continue
        values.append(value)
    return values


_SELLER_FAILURE = "Send_Seller_Msg_Failure"
_FAILURE_FIELDS = {"cmd", "command", "event", "eventname", "event_name"}


def is_seller_failure_line(line: str) -> bool:
    """Match the PDD failure protocol, never the same text inside chat content."""
    marker_at = line.find(_SELLER_FAILURE)
    json_at = min((index for index in (line.find("{"), line.find("[")) if index >= 0), default=-1)
    if marker_at >= 0 and (json_at < 0 or marker_at < json_at):
        return True

    def walk(value: Any, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in _FAILURE_FIELDS and str(child).strip() == _SELLER_FAILURE:
                    return True
                if isinstance(child, (dict, list)) and walk(child, depth + 1):
                    return True
        elif isinstance(value, list):
            return any(walk(child, depth + 1) for child in value)
        return False

    return any(walk(root) for root in _json_values(line))


def _parse_business_messages(line: str, source: str, account_hint: str) -> List[Dict[str, Any]]:
    """Parse only trusted PDD seller callback envelopes and payloads."""
    if is_seller_failure_line(line):
        return []

    candidates: Dict[tuple[str, str, str, str], Dict[str, Any]] = {}
    outer_match = TS_RE.search(line)
    outer_ts = _normalize_ts(outer_match.group("ts")) if outer_match else 0

    def message_id(value: dict, inherited: str = "") -> str:
        fields = {str(key).lower(): key for key in value}
        return str(
            value.get(fields.get("msgid", ""))
            or value.get(fields.get("messageid", ""))
            or inherited
            or ""
        ).strip()

    def walk_payload(value: Any, json_depth: int = 0, inherited_message_id: str = "") -> None:
        if json_depth > 3:
            return
        if isinstance(value, str):
            text = value.strip()
            if not text or text[0] not in "[{\"":
                return
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                return
            walk_payload(decoded, json_depth + 1, inherited_message_id)
            return
        if isinstance(value, list):
            for child in value:
                walk_payload(child, json_depth, inherited_message_id)
            return
        if not isinstance(value, dict):
            return

        fields = {str(key).lower(): key for key in value}
        own_message_id = message_id(value, inherited_message_id)
        required = {"sendcontent", "sellerid"}
        buyer_key = fields.get("buyid") or fields.get("buyerid")
        if required.issubset(fields) and buyer_key is not None:
            content = _repair_mojibake(_unescape(str(value.get(fields["sendcontent"]) or ""))).strip()
            buyer_id = str(value.get(buyer_key) or "").strip()
            account = _canonical_account(value.get(fields["sellerid"]) or account_hint)
            timestamp_key = fields.get("timestamp")
            explicit_ts = value.get(timestamp_key) if timestamp_key is not None else ""
            raw_ts = explicit_ts if explicit_ts not in (None, "") else (
                outer_match.group("ts") if outer_match else ""
            )
            message_ts = _normalize_ts(explicit_ts) if explicit_ts not in (None, "") else 0
            live_timestamp = not (outer_ts and message_ts) or abs(message_ts - outer_ts) <= 120
            if buyer_id and account and content and live_timestamp:
                platform_ts_key = _platform_time_key(raw_ts)
                output_message_id = own_message_id or "callback-" + _stable_hash(
                    account, buyer_id, _canonical_text(content), platform_ts_key
                )[:20]
                candidate_key = (account, buyer_id, _canonical_text(content), platform_ts_key)
                row = {
                    "msg_id": output_message_id,
                    "platform_message_id": own_message_id,
                    "identity_kind": "message_id" if own_message_id else "stable_hash",
                    "buyer_id": buyer_id,
                    "role": "mall_cs",
                    "content": content,
                    "ts": _normalize_ts(raw_ts),
                    "platform_ts_key": platform_ts_key,
                    "account": account,
                    "source": source,
                    "delivery_status": "confirmed",
                    "business_message": True,
                }
                previous = candidates.get(candidate_key)
                if previous is None or (own_message_id and not previous.get("platform_message_id")):
                    candidates[candidate_key] = row

        for child in value.values():
            if isinstance(child, (dict, list)):
                walk_payload(child, json_depth, own_message_id)

    def walk_envelope(value: Any, inherited_message_id: str = "") -> None:
        if isinstance(value, list):
            for child in value:
                walk_envelope(child, inherited_message_id)
            return
        if not isinstance(value, dict):
            return
        own_message_id = message_id(value, inherited_message_id)
        for key, child in value.items():
            if str(key).lower() == "origindata":
                walk_payload(child, inherited_message_id=own_message_id)
            elif isinstance(child, (dict, list)):
                walk_envelope(child, own_message_id)

    roots = [] if "buyer_msg=" in line else _json_values(line)
    for root in roots:
        walk_envelope(root)

    if "buyer_msg=" not in line:
        for origin in _json_string_field(line, "originData"):
            walk_payload(origin)

    marker_at = line.find("dllSendCallBack")
    if marker_at >= 0:
        payload_values = _json_values(line[marker_at + len("dllSendCallBack"):])
        if payload_values:
            walk_payload(payload_values[0])
    return list(candidates.values())


def _clean_context_text(value: Any, limit: int = 1000) -> str:
    return _canonical_text(_repair_mojibake(_unescape(str(value or ""))))[:limit]


def _first_context_value(sources: tuple[dict, ...], *keys: str) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return ""


def _nested_goods_id(value: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, dict):
        for key in ("goods_id", "goodsId", "goodsID"):
            candidate = str(value.get(key) or "").strip()
            if re.fullmatch(r"\d{6,20}", candidate):
                return candidate
        for key in (
            "button_click_action", "buttonClickAction", "click_action",
            "clickAction", "params", "button", "spellOrderData", "data",
            "goods_info", "goodsInfo",
        ):
            if key in value:
                candidate = _nested_goods_id(value.get(key), depth + 1)
                if candidate:
                    return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _nested_goods_id(item, depth + 1)
            if candidate:
                return candidate
    return ""


def _valid_structured_order_id(value: Any) -> str:
    """Accept only order IDs read from explicit JSON fields."""
    text = str(value or "").strip()
    if re.fullmatch(r"26\d{4}-\d{12,18}", text):
        return text
    if not re.fullmatch(r"\d{12,30}", text):
        return ""
    if text.startswith("89") and 18 <= len(text) <= 22:
        return ""
    return text


def _mask_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * (len(text) - 8) + text[-4:]


def _product_order_fields(message: dict, content: str) -> Dict[str, Any]:
    """Normalize PDD product cards and their optional explicit order context."""
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    card_data = info.get("data") if isinstance(info.get("data"), dict) else {}
    goods_info = info.get("goods_info") if isinstance(info.get("goods_info"), dict) else {}
    if not goods_info and isinstance(info.get("goodsInfo"), dict):
        goods_info = info.get("goodsInfo")
    # ``user_source`` messages put the consulted product under info.goods_info.
    # It must win over info.title, which only says "current user came from product page".
    sources = (goods_info, card_data, info)
    result: Dict[str, Any] = {}

    goods_name = _clean_context_text(
        _first_context_value(sources, "goodsName", "goods_name", "title", "goods_title"),
        500,
    )
    goods_spec = _clean_context_text(
        _first_context_value(sources, "goodsSpec", "goods_spec", "spec", "sku_name", "skuName"),
        500,
    )
    link = _clean_context_text(
        _first_context_value(
            sources,
            "linkUrl", "link_url", "url", "goods_url", "goodsUrl", "mall_link_url",
        ),
        2000,
    )
    thumb = _clean_context_text(
        _first_context_value(
            sources,
            "goodsThumbUrl", "goods_thumb_url", "thumbUrl", "thumb_url", "image", "imageUrl",
        ),
        2000,
    )
    price_raw = _first_context_value(
        sources,
        "goodsPrice", "price", "min_group_price", "goods_price", "total_amount",
    )
    goods_price = _clean_context_text(price_raw, 100)
    cents_value = ""
    if goods_info and "total_amount" in goods_info:
        cents_value = goods_info.get("total_amount")
    elif card_data and "goods_price" in card_data:
        cents_value = card_data.get("goods_price")
    if cents_value not in (None, ""):
        try:
            if isinstance(cents_value, (int, float)) or str(cents_value).strip().isdigit():
                goods_price = f"{float(cents_value) / 100:.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            pass

    normalized_content = _content_value(content)
    goods_id = ""
    for candidate_text in (normalized_content, link):
        for pattern in (GOODS_URL_RE, GOODS_ID_RE):
            match = pattern.search(candidate_text)
            if match:
                goods_id = str(match.group(1) or "").strip()
                break
        if goods_id:
            break
    if not goods_id:
        goods_id = _nested_goods_id(card_data) or _nested_goods_id(info)

    if link and not link.lower().startswith(("http://", "https://")):
        link = f"https://mobile.yangkeduo.com/{link.lstrip('/')}"
    goods_url = link if link.lower().startswith(("http://", "https://")) else ""
    if normalized_content.lower().startswith(("http://", "https://")) and "goods_id=" in normalized_content.lower():
        goods_url = normalized_content
    if not goods_url and goods_id:
        goods_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
    if thumb and not thumb.lower().startswith(("http://", "https://")):
        thumb = ""

    info_key = str(info.get("key") or "").strip().lower()
    is_goods_card = bool(
        goods_name
        or goods_id
        or "goods" in info_key
        or "goods.html" in normalized_content.lower()
        or "goods_id=" in normalized_content.lower()
    )
    template_name = str(message.get("template_name") or info.get("template_name") or "").strip()
    if is_goods_card and not template_name:
        template_name = "user_goods_card"
    if template_name:
        result["template_name"] = template_name
    try:
        result["raw_type"] = int(message.get("type") or 0)
    except (TypeError, ValueError):
        result["raw_type"] = 0

    if is_goods_card:
        product_fields = {
            "goods_id": goods_id,
            "goods_name": goods_name,
            "goods_url": goods_url,
            "goods_thumb_url": thumb,
            "goods_price": goods_price,
            "goods_spec": goods_spec,
        }
        result.update({key: value for key, value in product_fields.items() if value})
        parts: List[str] = []
        if goods_name:
            parts.append(goods_name)
        if goods_spec and goods_spec not in goods_name:
            parts.append(f"\u89c4\u683c {goods_spec}")
        if goods_price:
            parts.append(f"\u4ef7\u683c {goods_price}")
        if goods_url:
            parts.append(goods_url)
        if (
            normalized_content
            and not normalized_content.lower().startswith(("http://", "https://"))
            and normalized_content not in parts
            and "goods_id=" not in normalized_content.lower()
        ):
            parts.append(normalized_content)
        if parts:
            result["content"] = "\n".join(parts)

    platform_order_no = _valid_structured_order_id(_first_context_value(
        sources,
        "orderSequenceNo", "order_sequence_no", "orderSequenceNumber", "orderSn",
    ))
    internal_order_id = str(
        _first_context_value(sources, "order_id", "orderId", "orderID") or ""
    ).strip()
    if platform_order_no or internal_order_id:
        order_info: Dict[str, Any] = {
            "source": "pdd_goods_card_info",
            "context_received": True,
            "order_count": 1,
            "ambiguous": False,
        }
        if platform_order_no:
            order_info["order_id_masked"] = _mask_identifier(platform_order_no)
            order_info["platform_order_no_masked"] = _mask_identifier(platform_order_no)
            result["order_id"] = platform_order_no
        if internal_order_id:
            order_info["internal_order_id_masked"] = _mask_identifier(internal_order_id)
        safe_fields = {
            "goods_id": ("goods_id", "goodsId", "goodsID"),
            "order_status": ("order_status", "orderStatus"),
            "shipping_status": ("shipping_status", "shippingStatus"),
            "pay_status": ("pay_status", "payStatus"),
        }
        for output_key, aliases in safe_fields.items():
            value = _first_context_value(sources, *aliases)
            if value not in (None, ""):
                order_info[output_key] = _clean_context_text(value, 200)
        result["order_info"] = order_info
    return result


def _structured_order_context(value: Any) -> Dict[str, Any]:
    """Extract explicit product/order fields from ImWs order context only."""
    records: List[dict] = []
    seen: set[int] = set()

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(item, str):
            for decoded in _json_values(item):
                if decoded != item:
                    walk(decoded, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
            return
        if not isinstance(item, dict) or id(item) in seen:
            return
        seen.add(id(item))
        explicit_keys = {
            "orderSequenceNo", "order_sequence_no", "orderSequenceNumber", "orderSn",
            "order_id", "orderId", "orderID", "goods_id", "goodsId", "goodsID",
            "goodsName", "goods_name", "goodsThumbUrl", "goods_thumb_url",
        }
        if explicit_keys.intersection(item):
            records.append(item)
        for child in item.values():
            if isinstance(child, (dict, list, str)):
                walk(child, depth + 1)

    walk(value)
    if not records:
        return {}
    fields = _product_order_fields({"info": records[0]}, "")
    order_ids: List[str] = []
    for record in records:
        sources = (record,)
        order_id = _valid_structured_order_id(_first_context_value(
            sources,
            "orderSequenceNo", "order_sequence_no", "orderSequenceNumber", "orderSn",
        ))
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
    if order_ids:
        fields["order_id"] = order_ids[0] if len(order_ids) == 1 else ""
        order_info = dict(fields.get("order_info") or {})
        order_info.update({
            "source": "pdd_imws_order_context",
            "context_received": True,
            "order_count": len(order_ids),
            "ambiguous": len(order_ids) > 1,
        })
        order_info["order_ids_masked"] = [_mask_identifier(item) for item in order_ids[:5]]
        fields["order_info"] = order_info
        if not fields["order_id"]:
            fields.pop("order_id", None)
    return fields


def _order_context(value: Any) -> Dict[str, Any]:
    """Return bounded optional context; order data never gates message parsing."""
    if value in (None, "", [], {}):
        return {}
    if isinstance(value, str):
        decoded = _json_values(value)
        value = decoded[0] if decoded else value
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= 4096:
            return {"raw": value}
        return {"present": True, "truncated": True}
    return {"present": True}


def _parse_imws_line(line: str, source: str, account_hint: str) -> List[Dict[str, Any]]:
    markers = ("拼多多-ImWs-上报聊天消息成功", "firstLineOrderList", "orderInfoListJson")
    if not any(marker in line for marker in markers):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any, depth: int = 0, inherited_account: str = "") -> None:
        if depth > 14:
            return
        if isinstance(value, str):
            text = _unescape(value).strip()
            content_index = max(text.rfind("内容:"), text.rfind("内容："))
            candidates = [text]
            if content_index >= 0:
                candidates.insert(0, text[content_index + 3:].strip())
            for candidate in candidates:
                for decoded in _json_values(candidate):
                    walk(decoded, depth + 1, inherited_account)
            return
        if isinstance(value, list):
            for child in value:
                walk(child, depth + 1, inherited_account)
            return
        if not isinstance(value, dict):
            return

        explicit_account = _canonical_account(_profile_value(value, "account"))
        account = explicit_account or inherited_account or account_hint
        body = value.get("body") if isinstance(value.get("body"), dict) else None
        if body is not None:
            source_ref = str(_profile_value(value, "source_ref") or "").strip().upper()
            role = str((_profile_snapshot().get("role_map") or {}).get(source_ref) or "").strip()
            if source_ref == "BUYER" and role == "user":
                buyer_id = str(_profile_value(value, "buyer_id") or "").strip()
                content = _content_value(_profile_value(value, "content"))
                if buyer_id and content:
                    platform_id = str(_profile_value(value, "message_id") or "").strip()
                    request_id = str(_profile_value(value, "request_id") or "").strip()
                    create_time = _profile_value(value, "create_time")
                    message_time = _profile_value(value, "message_time")
                    identity_kind = "message_id"
                    msg_id = platform_id
                    if not msg_id:
                        identity_kind = "req_id"
                        msg_id = request_id
                    if not msg_id:
                        identity_kind = "create_time"
                        msg_id = str(create_time or "").strip()
                    if not msg_id:
                        identity_kind = "stable_hash"
                        msg_id = f"imws-{_stable_hash(account, buyer_id, content, message_time)[:20]}"
                    fingerprint = _stable_hash(account, buyer_id, msg_id, role, content)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        context = _order_context(_profile_value(value, "order_context"))
                        row = {
                            "msg_id": msg_id,
                            "platform_message_id": platform_id,
                            "identity_kind": identity_kind,
                            "buyer_id": buyer_id,
                            "role": "user",
                            "content": content,
                            "ts": _normalize_ts(message_time or create_time),
                            "platform_ts_key": _platform_time_key(message_time or create_time),
                            "buyer_nick": _canonical_text(_profile_value(value, "buyer_nick")),
                            "shop_name": _canonical_text(_profile_value(value, "shop_name")),
                            "account": _canonical_account(account),
                            "source": str(source or "logrus").lower(),
                            "delivery_status": "",
                        }
                        if context:
                            row["order_context"] = context
                            row.update(_structured_order_context(_profile_value(value, "order_context")))
                        out.append(row)
        for child in value.values():
            if isinstance(child, (dict, list, str)):
                walk(child, depth + 1, account)

    for decoded in _json_values(line):
        walk(decoded, inherited_account=account_hint)
    return out


def parse_line(line: str, source: str = "plugin") -> List[Dict[str, Any]]:
    if not is_candidate_line(line) or is_seller_failure_line(line):
        return []

    account_hint = _extract_account(line)
    results: List[Dict[str, Any]] = []
    blobs: List[str] = []

    results.extend(_parse_imws_line(line, source, account_hint))
    results.extend(_parse_business_messages(line, source, account_hint))

    for match in re.finditer(r'originData"\s*:\s*"(?P<j>(?:\\.|[^"\\])*)"', line):
        blobs.append(_unescape(match.group("j")))
    for match in re.finditer(r"buyer_msg=(?P<j>\{.*\})", line):
        blobs.append(match.group("j"))
    if "dllRecvCallBack" in line:
        idx = line.find("{")
        if idx >= 0:
            blobs.append(line[idx:])

    if any(x in line for x in LOGRUS_MARKERS):
        content_m = re.search(r"(?:消息内容|娑堟伅鍐呭):\[(.*?)\]", line)
        mid_m = re.search(r"(?:消息ID|娑堟伅ID):\[([^\]]*)\]", line)
        buyer_m = re.search(
            r'(?:buyerAccount|buyer_id|buyerId|userId)["\s:=]+["\[]?(?P<buyer>[A-Za-z0-9_-]{2,128})',
            line,
        )
        sender_m = re.search(
            r"(?:发送者|鍙戦€佽€?)[：:]\[(?P<uid>[^,\]]+),(?P<role>user|mall_cs)\]",
            line,
            re.I,
        )
        receiver_m = re.search(
            r"(?:接收者|鎺ユ敹鑰?)[：:]\[(?P<uid>[^,\]]+),(?P<role>user|mall_cs)\]",
            line,
            re.I,
        )
        if not buyer_m:
            buyer_m = re.search(
                r'"role"\s*:\s*"user"[^\n]{0,240}?"uid"\s*:\s*"?(?P<buyer>[A-Za-z0-9_-]{2,128})',
                line,
            )
        buyer_id = buyer_m.group("buyer") if buyer_m else ""
        role = "user"
        if not buyer_id and sender_m:
            sender_uid = sender_m.group("uid").strip()
            sender_role = sender_m.group("role").lower()
            receiver_uid = receiver_m.group("uid").strip() if receiver_m else ""
            receiver_role = receiver_m.group("role").lower() if receiver_m else ""
            sender_is_seat = bool(ACCOUNT_RE.search(sender_uid) or PLAIN_ACCOUNT_RE.fullmatch(sender_uid))
            if sender_role == "user" and not sender_is_seat:
                # The uid is usable only when the log explicitly labels it as a user.
                buyer_id = sender_uid
                role = "user"
            elif sender_role == "mall_cs" and receiver_role == "user" and receiver_uid:
                buyer_id = receiver_uid
                role = "mall_cs"
        if content_m and buyer_id:
            message_ids = [part.strip() for part in (mid_m.group(1).split(",") if mid_m else [])]
            platform_message_id = next((part for part in message_ids if part), "")
            send_time_m = re.search(r"(?:发送时间|鍙戦€佹椂闂?)[：:]\[([0-9.]+)\]", line)
            raw_ts = send_time_m.group(1) if send_time_m else (
                (TS_RE.search(line).group("ts") if TS_RE.search(line) else None)
                if role != "user" else None
            )
            results.append(
                {
                    "msg_id": platform_message_id or f"logrus-{_stable_hash(line)[:16]}",
                    "platform_message_id": platform_message_id,
                    "identity_kind": "message_id" if platform_message_id else "stable_hash",
                    "buyer_id": buyer_id,
                    "role": role,
                    "content": content_m.group(1),
                    "ts": _normalize_ts(raw_ts),
                    "account": account_hint,
                    "source": source,
                    "delivery_status": "",
                    "platform_ts_key": _platform_time_key(raw_ts),
                }
            )

    for blob in blobs:
        results.extend(_parse_blob(blob, source, account_hint))

    # Plugin success lines (拼多多 only — 千牛回执会污染成短 buyer_id 第二会话)
    if "Send_Seller_Msg_Success" in line or "Send_Robot_Msg" in line:
        bid = re.search(r"buyer_id:(\d+)", line)
        acc = re.search(r"cs_id:([^\s]+)", line)
        # Stop at protocol fields that often trail utf8_msg in mixed logs
        msg = re.search(
            r"utf8_msg:(.*?)(?:\s+(?:msg_type|text_or_picture|is_light_up|is_read_second|result):|$)",
            line,
        )
        account = _canonical_account(acc.group(1) if acc else account_hint)
        # Skip non-PDD seats (旺旺 nick / empty) — those belong to Qianniu bridge
        if account and not str(account).startswith("cs_"):
            account = ""
        if bid and account.startswith("cs_") and msg:
            content = _canonical_text(msg.group(1) if msg else "")
            # Drop protocol pollution tails if regex still leaked
            content = re.sub(
                r"\s+(?:text_or_picture|is_light_up|is_read_second)\s*[:=].*$",
                "",
                content,
                flags=re.I,
            ).strip()
            if content and "text_or_picture" not in content.lower():
                results.append(
                    {
                        "msg_id": f"plugin-send-{_stable_hash(line)[:16]}",
                        "buyer_id": bid.group(1),
                        "role": "mall_cs",
                        "content": content,
                        "ts": _normalize_ts(TS_RE.search(line).group("ts") if TS_RE.search(line) else time.time()),
                        "account": account,
                        "source": source,
                        "delivery_status": "confirmed",
                    }
                )

    unique: Dict[str, Dict[str, Any]] = {}
    for msg in results:
        if not msg.get("buyer_id") or not msg.get("content"):
            continue
        if msg.get("business_message"):
            msg["content"] = _repair_mojibake(str(msg.get("content") or "")).strip()
        else:
            msg["content"] = _canonical_text(_repair_mojibake(str(msg.get("content") or "")))
        if not msg["content"]:
            continue
        # Drop residual double-encoding junk
        if "Ã" in msg["content"] and sum(1 for c in msg["content"] if "\u4e00" <= c <= "\u9fff") == 0:
            continue
        if not msg.get("account"):
            msg["account"] = account_hint
        platform_message_id = str(msg.get("platform_message_id") or "").strip()
        platform_time = str(msg.get("platform_ts_key") or msg.get("ts") or "").strip()
        if platform_message_id:
            key = _stable_hash("platform-message-id", platform_message_id)
        else:
            key = _stable_hash(
                "message-fallback",
                msg.get("account"),
                msg.get("buyer_id"),
                msg.get("role"),
                _canonical_text(msg.get("content")),
                platform_time,
            )
        msg["dedupe_key"] = key
        msg["idempotency_key"] = key[:32]
        unique[key] = msg
    return list(unique.values())


def _parse_blob(blob: str, source: str, account_hint: str) -> List[Dict[str, Any]]:
    text = blob.strip()
    if not text.startswith(("{", "[")):
        return []
    try:
        data = json.loads(text)
    except Exception:
        try:
            data = json.loads(_unescape(text))
        except Exception:
            return []

    out: List[Dict[str, Any]] = []
    seen_nodes: set[int] = set()

    def walk(
        value: Any,
        depth: int = 0,
        inherited_target_id: str = "",
        inherited_account: str = "",
    ) -> None:
        if depth > 12:
            return
        if isinstance(value, str):
            nested_text = _unescape(value).strip()
            if nested_text.startswith(("{", "[")):
                try:
                    walk(json.loads(nested_text), depth + 1, inherited_target_id, inherited_account)
                except Exception:
                    pass
            return
        if isinstance(value, list):
            for child in value:
                walk(child, depth + 1, inherited_target_id, inherited_account)
            return
        if not isinstance(value, dict):
            return
        node_id = id(value)
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)

        if is_pdd_system_message(value):
            return

        target_id = str(
            value.get("target_id") or value.get("targetId") or inherited_target_id or ""
        ).strip()
        current_account = _canonical_account(
            value.get("cs_id")
            or value.get("account")
            or value.get("mall_cs_id")
            or inherited_account
            or account_hint
        )

        from_obj = value.get("from") if isinstance(value.get("from"), dict) else {}
        to_obj = value.get("to") if isinstance(value.get("to"), dict) else {}
        raw_content = (
            value.get("content")
            if value.get("content") not in (None, "")
            else value.get("text")
            if value.get("text") not in (None, "")
            else value.get("msg")
            if not isinstance(value.get("msg"), (dict, list))
            else ""
        )
        if isinstance(raw_content, (dict, list)):
            content = json.dumps(raw_content, ensure_ascii=False, separators=(",", ":"))
        else:
            content = _canonical_text(_repair_mojibake(raw_content))

        role_raw = str(value.get("role") or from_obj.get("role") or "").lower()
        if role_raw in {"user", "buyer"}:
            role = "user"
        elif role_raw in {"mall_cs", "cs", "seller", "assistant"}:
            role = "mall_cs"
        else:
            role = ""

        buyer = str(
            value.get("buyerAccount")
            or value.get("buyer_id")
            or value.get("buyerId")
            or value.get("userId")
            or value.get("buyId")
            or value.get("BuyId")
            or value.get("user_id")
            or ""
        ).strip()
        if not buyer and role == "user":
            buyer = str(from_obj.get("uid") or value.get("uid") or "").strip()
        elif not buyer and role == "mall_cs":
            buyer = str(to_obj.get("uid") or value.get("to_uid") or "").strip()
        if not buyer and role == "user":
            user = value.get("user") if isinstance(value.get("user"), dict) else {}
            buyer = str(user.get("uid") or user.get("userId") or "").strip()

        if buyer and content and role:
            context_fields = _product_order_fields(value, content)
            content = str(context_fields.pop("content", content) or content)
            platform_message_id = str(
                value.get("messageId")
                or value.get("msg_id")
                or value.get("message_id")
                or value.get("msgId")
                or value.get("client_msg_id")
                or value.get("id")
                or ""
            ).strip()
            msg_id = platform_message_id
            account = current_account
            if not account and target_id.isdigit():
                if role == "user" and str(to_obj.get("role") or "").lower() == "mall_cs":
                    mall_id = str(to_obj.get("mall_id") or to_obj.get("uid") or "").strip()
                elif role == "mall_cs" and str(from_obj.get("role") or "").lower() == "mall_cs":
                    mall_id = str(from_obj.get("mall_id") or from_obj.get("uid") or "").strip()
                else:
                    mall_id = ""
                if mall_id.isdigit():
                    account = f"cs_{mall_id}:{target_id}"
            if not msg_id:
                msg_id = f"blob-{_stable_hash(buyer, content, value.get('ts'))[:16]}"
            raw_ts = value.get("ts") or value.get("time") or value.get("timestamp")
            row = {
                    "msg_id": msg_id,
                    "platform_message_id": platform_message_id,
                    "buyer_id": buyer,
                    "role": role,
                    "content": content,
                    "ts": _normalize_ts(raw_ts),
                    "platform_ts_key": _platform_time_key(raw_ts),
                    "pre_msg_id": str(
                        value.get("pre_msg_id")
                        or value.get("preMsgId")
                        or value.get("parent_msg_id")
                        or ""
                    ).strip(),
                    "buyer_nick": _canonical_text(_repair_mojibake(
                        value.get("buyerNick") or value.get("buyer_nick") or value.get("nickname") or ""
                    )),
                    "shop_name": _canonical_text(_repair_mojibake(
                        value.get("mallName") or value.get("shopName") or value.get("mall_name") or ""
                    )),
                    "account": account,
                    "source": source,
                    "delivery_status": "",
                    "message_type": value.get("type"),
                    "template_name": str(
                        context_fields.get("template_name") or value.get("template_name") or ""
                    ),
                    "raw_type": context_fields.get("raw_type", value.get("type")),
                    "no_unreply_hint": bool(value.get("no_unreply_hint")),
                    "conv_silent": bool(value.get("conv_silent")),
                }
            row.update(context_fields)
            out.append(row)

        for child in value.values():
            if isinstance(child, (dict, list, str)):
                walk(child, depth + 1, target_id, current_account)

    walk(data)
    return out
