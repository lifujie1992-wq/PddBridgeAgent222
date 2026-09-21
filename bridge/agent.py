# -*- coding: utf-8 -*-
"""Bridge agent runtime loop (multi-platform)."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
import urllib.request
import traceback
from collections import OrderedDict, deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import __build_hash__, __version__
from .client import BridgeClient, BridgeClientError
from .center_ws import (DEFAULT_WS_PATH, CenterEventChannel, CenterWsUnavailable,
                        ws_url_from_server_url)
from .command_journal import CommandJournal
from .event_queue import append_event
from .config import as_bool, load_config, save_config, write_example_config
from .platforms import get_platform
from .watcher import LogWatcher
from .seat_scope import SCOPE_BLOCK, SeatScope
from .message_timing import (TIMING_FIELDS, FUTURE_TOLERANCE_SECONDS,
                             TAKEOVER_PARENT_MAX_AGE_SECONDS, epoch_seconds,
                             stamp_message, history_ready, command_expired, queue_metrics)

log = logging.getLogger("pdd.bridge")

# WS 通道保留多少条最近事件用于 result 落死信（result 只推给在线连接，无需长期留）
_WS_RECENT_LIMIT = 5000

# 出站内容归一化：去掉 emoji/标点/空白，只留字母数字与汉字，用于判定“同一次发送”
_OUTGOING_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

# 跨数据源入站判重窗口的下限（秒）。PDD 上有**两条腿**同时收消息：探域日志（实时）
# 和 CDP 周期回拉（补漏）。两条腿各自维护判重表、互不知情，同一条买家消息会被各报
# 一次 —— 实测间隔 15~18s，台账里同一个 event_id 出现两条 captured。中心按 event_id
# 幂等去了重，所以代价只是流量翻倍 + 日志噪音，买家不会被打扰两次。
# 账本必须放在两条腿的汇合点（本文件的 _on_local_event），窗口要盖过回拉间隔。
_INGEST_DEDUP_MIN_SECONDS = 600.0
_INGEST_DEDUP_PULL_FACTOR = 5.0
# 表涨到这么多才考虑清理（清理按窗口 cutoff 判定，清掉的条目永远不可能再命中）
_INGEST_DEDUP_PRUNE_THRESHOLD = 5000
_INGEST_DEDUP_PRUNE_INTERVAL_SECONDS = 60.0


def _ingest_window_seconds(cfg: dict) -> float:
    """跨数据源判重窗口（秒）。

    自动值 = max(下限, history_pull_seconds × 倍数)。窗口必须盖过 CDP 腿的回拉间隔，
    否则两条腿报同一条时账本已经过期，照样双投。配置里显式给了就用配置值，但不允许
    短于下限 —— 短了等于没有判重。
    """
    try:
        pull_seconds = float(cfg.get("history_pull_seconds") or 0.0)
    except (TypeError, ValueError):
        pull_seconds = 0.0
    auto = max(_INGEST_DEDUP_MIN_SECONDS, pull_seconds * _INGEST_DEDUP_PULL_FACTOR)
    try:
        window = float(cfg.get("cross_source_dedup_seconds") or auto)
    except (TypeError, ValueError):
        window = auto
    return max(window, _INGEST_DEDUP_MIN_SECONDS)


def _build_seat_scope(agent) -> SeatScope:
    """建本机席位集合。provider 从 CDP 会话取账号（= 工作台打开的标签页）。"""
    def _accounts():
        """本机席位：CDP 会话 ∪ 网关学到的 seat_accounts。

        **为什么不能只看 CDP**：CDP 扫的是"聊天标签页开着的"店铺，不等于
        "我登录的店铺"。生产事故（2026-09-21 真实机器）就是买家消息的席位
        没被 CDP 探测到 -> 被判"不是本机" -> 拦下 -> 浮窗不显示 -> 超时。
        `seat_accounts` 是网关从真实流过的消息里学到的本工位账号，作为补充。
        """
        out = []
        source = getattr(agent, "pddbridge_source", None)
        for session in list(getattr(source, "sessions", None) or []):
            account = str(getattr(session, "account", "") or "").strip()
            if account:
                out.append(account)
        try:
            cfg_path = Path(str((getattr(agent, "cfg", None) or {}).get("config_path")
                               or "bridge_config.json"))
            state_path = cfg_path.resolve().parent / "data" / "seat_local_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            for account in (state.get("seat_accounts") or {}).values():
                account = str(account or "").strip()
                if account:
                    out.append(account)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return out

    return SeatScope(getattr(agent, "cfg", None) or {}, accounts_provider=_accounts)


def _taobao_store_account(account: Any) -> str:
    """Return the store portion of a Taobao ``store:staff`` login name."""
    account = str(account or "").strip()
    separators = [index for index in (account.find(":"), account.find("：")) if index >= 0]
    if not separators:
        return account
    return account[:min(separators)].strip() or account


def _command_result_for_ack(result: dict) -> dict:
    """Never acknowledge an unverified send as a successful command."""
    result = dict(result or {})
    if str(result.get("status") or "").lower() == "indeterminate":
        result["ok"] = False
        result["delivery_uncertain"] = True
        result["retryable"] = False
    return result


def _sent_text_key(value: Any) -> str:
    """发送内容归一化：只留中英文/数字。

    探域日志会把 emoji 变成 `??` 或乱码，还会插换行，所以比之前先去掉这些噪声。
    """
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or ""))


def _same_sent_text(ours: Any, theirs: Any) -> bool:
    """日志里那条 mall_cs 消息，是不是我们（中心指令 / AI）刚发出去的那条。

    日志常被截断（只留第一行）或把 emoji 变成乱码，所以不能只比全等：
    短的那条若是长的前缀，或相似度够高，都算同一条。
    """
    a, b = _sent_text_key(ours), _sent_text_key(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 6 and long_.startswith(short):
        return True
    if len(short) >= 12 and short[:8] in long_ and short[-6:] in long_:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.6


def _same_seat(left: Any, right: Any) -> bool:
    """两个账号是不是同一个席位（容忍 cs_商城:席位 / mall_商城 等写法差异）。"""
    a, b = str(left or "").strip(), str(right or "").strip()
    if not a or not b:
        return True   # 缺一边就不拦，交给内容判定
    if a == b:
        return True
    da = re.findall(r"\d{6,}", a)
    db = re.findall(r"\d{6,}", b)
    return bool(da) and bool(db) and bool(set(da) & set(db))


class BridgeAgent:
    # 保护"惰性建实例锁"这一步的类级锁（见 _claim_command 的双检锁说明）。
    _claim_init_lock = threading.Lock()
    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or load_config()
        self.platform = get_platform(self.cfg.get("platform") or "pdd")
        self.client = BridgeClient(
            self.cfg["server_url"],
            self.cfg["agent_token"],
            self.cfg["agent_id"],
            self.cfg.get("agent_name") or "",
            self.cfg.get("device_id") or "",
        )
        # 中心 WS 上行通道（可选，默认关）。只替换 upload_events；register /
        # heartbeat / 指令长轮询 / 指令结果回报仍走 HTTP —— 服务端目前只实现了
        # 上行 event + ack/result，下行指令没有 WS 通道。
        self.center_ws = self._build_center_ws()
        # result 帧回来看不到原始事件(已出队)，这里留一份有界的最近事件用于落死信。
        self._ws_recent: "OrderedDict[str, dict]" = OrderedDict()
        self._ws_recent_lock = threading.Lock()
        self._stop = threading.Event()
        self._pending: Deque[dict] = deque()
        self._pending_lock = threading.Lock()
        self._accounts_seen: Dict[str, float] = {}
        self._scope_filtered_events = 0
        self._scope_blocked_commands = 0
        self._last_heartbeat_ok = False
        self._last_error = ""
        self._last_status: dict = {}
        self._commands_done: set[str] = set()
        # 命令通道的可观测计数。原来"有没有收到指令"在日志和心跳里都看不出来：
        # 长轮询失败只写内存、指令缺 id 直接丢，出了事只能靠翻日志猜（实测吃过亏）。
        self._cmd_stats: Dict[str, Any] = {
            "poll_ok": 0, "poll_fail": 0, "received": 0, "sent_ok": 0,
            "no_id": 0, "last_received_at": 0.0, "last_error": "",
        }
        self._cmd_warn_at: Dict[str, float] = {}
        # 投递台账: 每条消息的 采集/本地工作台/中心 三个环节逐条留痕
        from .ledger import DeliveryLedger
        queue_file = str(self.cfg.get("local_queue_path") or "bridge_queue.jsonl")
        self._ledger = DeliveryLedger(
            self.cfg.get("delivery_ledger_path") or queue_file.replace(".jsonl", "_ledger.jsonl"),
            enabled=bool(self.cfg.get("delivery_ledger_enabled", True)),
        )

        # 跨数据源入站判重：两条腿（探域日志 watcher / CDP 周期回拉）各有各的判重表，
        # 同一条买家消息会被两边各报一次（实测间隔 15~18s）。这里按平台消息 id 记一本
        # 共用账本，放在两条腿唯一的汇合点。窗口自动取 max(下限, 回拉间隔 × 倍数)。
        self._ingest_seen: Dict[str, float] = {}
        self._ingest_stats: Dict[str, Any] = {"dropped": 0, "last_drop_at": 0.0}
        self._ingest_window = _ingest_window_seconds(self.cfg)
        self.watcher = LogWatcher(
            self.cfg["tanyu_log_dir"],
            self._on_local_event,
            poll_interval_ms=int(self.cfg.get("poll_interval_ms") or 400),
            platform=self.platform.name,
            checkpoint_path=Path(self.cfg.get("local_queue_path") or "bridge_queue.jsonl").with_suffix(".cursors.json"),
        )
        # 本地工作台推送：收的消息除了上传中心，同时推给本机 gateway(18767) 的
        # /api/local-seat/v1/events（X-Agent-Token 鉴权）。这样中心连不上/令牌绑定时
        # 本地工作台仍能实时显示。失败只记日志，绝不影响 CDP/日志主链路。
        self._local_ingest_url = ""
        self._local_ingest_queue: Optional["queue.Queue[dict]"] = None
        local_workbench = str(
            self.cfg.get("local_workbench_url") or "http://127.0.0.1:18767"
        ).rstrip("/")
        if not getattr(type(self), "_local_first_v0512", False) and str(
            self.cfg.get("local_seat_push") or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}:
            self._local_ingest_url = f"{local_workbench}/api/local-seat/v1/events"
            self._local_ingest_queue = queue.Queue(maxsize=2000)
            threading.Thread(
                target=self._local_ingest_loop,
                name="local-seat-push",
                daemon=True,
            ).start()
        # CDP 实时数据源（PDD）：默认 data_source=cdp, 脱离探域日志; 可切回 tanyu_logs 降级
        self._data_source = str(self.cfg.get("data_source") or "cdp")
        self._effective_source = self._data_source
        self.pddbridge_source = None
        if self.platform.name == "pdd":
            try:
                from .pddbridge_source import PddbridgeSource

                self.pddbridge_source = PddbridgeSource(
                    self.cfg,
                    on_event=self._on_local_event,
                    on_fallback=self._fallback_to_tanyu_logs,
                    on_recover=self._recover_to_cdp,
                )
            except Exception as exc:
                log.warning("PddbridgeSource 构造失败, 使用探域日志数据源: %s", exc)
                self._data_source = "tanyu_logs"
                self._effective_source = "tanyu_logs"
        self.queue_path = Path(self.cfg.get("local_queue_path") or "bridge_queue.jsonl")
        self._load_local_queue()
        journal_path = self.cfg.get("command_journal_path") or self.queue_path.with_suffix(".commands.json")
        self.command_journal = CommandJournal(journal_path)
        if self.command_journal.last_error:
            self._last_error = self.command_journal.last_error

    @staticmethod
    def _ensure_event_id(event: dict) -> dict:
        event = dict(event)
        event_id = str(event.get("event_id") or event.get("idempotency_key") or "").strip()
        if not event_id and event.get("platform_message_id"):
            identity = "platform-message-id|" + str(event["platform_message_id"])
            event_id = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:32]
        if not event_id:
            identity = "\x00".join(str(event.get(key) or "") for key in (
                "platform", "account", "buyer_id", "msg_id", "role", "content", "ts"
            ))
            event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        event["event_id"] = event_id
        event["idempotency_key"] = event_id
        return event

    def _account_shop_id(self, account: Any) -> str:
        account = str(account or "").strip()
        if not account:
            return ""
        shop_map = self.cfg.get("shop_map") if isinstance(self.cfg.get("shop_map"), dict) else {}
        mapped = str(shop_map.get(account) or "").strip()
        if mapped:
            if self.platform.name == "taobao" and mapped.startswith("tb_nick_"):
                store_account = _taobao_store_account(account)
                if store_account != account:
                    return f"tb_nick_{store_account}"
            return mapped
        if account.startswith("cs_"):
            mall = account[3:].split(":", 1)[0].split("_", 1)[0]
            return f"mall_{mall}" if mall.isdigit() else ""
        if account.startswith(("mall_", "tb_")):
            return account
        if self.platform.name == "taobao":
            return f"tb_nick_{_taobao_store_account(account)}"
        return ""

    def _account_allowed(self, account: Any) -> bool:
        """The bridge is a transport; shop authorization belongs to the center."""
        return True

    def _load_local_queue(self) -> None:
        if not self.queue_path.is_file():
            return
        recovered = 0
        try:
            with self.queue_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(raw, dict):
                        continue
                    self._pending.append(self._ensure_event_id(stamp_message(raw, historical=True)))
                    self._remember_source_time(raw)
                    recovered += 1
        except OSError as exc:
            self._last_error = f"load_local_queue: {exc}"
            log.warning("load local event queue failed: %s", exc)
        if recovered:
            log.info("recovered %s unacknowledged bridge event(s)", recovered)

    def _remember_source_time(self, message: dict) -> None:
        if not hasattr(self, "_source_message_times"):
            self._source_message_times = {}
        key = (str(message.get("account") or ""), str(message.get("buyer_id") or ""),
               str(message.get("msg_id") or ""))
        if not self._source_message_times.get(key):
            self._source_message_times[key] = epoch_seconds(message.get("ts"))
        if len(self._source_message_times) > 20000:
            self._source_message_times.pop(next(iter(self._source_message_times)))

    def _command_expired(self, command: dict) -> bool:
        now = self.client.server_now() if hasattr(self.client, "server_now") else time.time()
        if command_expired(command, now):
            return True
        meta = command.get("meta") if isinstance(command.get("meta"), dict) else {}
        parent_id = str(meta.get("takeover_parent_msg_id") or "")
        if meta.get("manual_direct") is True or not parent_id:
            return False
        key = (str(command.get("account") or "").strip(), str(command.get("buyer_id") or "").strip(), parent_id)
        timestamp = getattr(self, "_source_message_times", {}).get(key, 0)
        if not timestamp:
            # 本地没有父消息时间（桥接重启后未回填、或父消息未经过本通道）。
            # 命令自身的 created_at 已做过 90s 时效校验，这里放行，避免静默丢 AI 回复。
            log.warning("takeover parent %s 本地无来源时间，按命令 created_at 放行", parent_id)
            return False
        if timestamp > now + FUTURE_TOLERANCE_SECONDS:
            return True
        return now - timestamp > TAKEOVER_PARENT_MAX_AGE_SECONDS

    def _merge_outgoing_duplicate(self, event: dict) -> bool:
        """出站(mall_cs)一条消息常被记 2–3 条：plugin-send / 平台回显 / DLL 回调；
        内容因 emoji 处理不同而哈希不同，稳定哈希去不掉。这里按 (account,buyer) +
        短时间窗 + 内容互为前缀合并，保留“更完整”的一条（带平台 msg_id 优先，其次内容更长）。

        返回 True 表示本条应被丢弃；若本条取代了更早的一条，会把它从待发队列移除。
        """
        if event.get("role") != "mall_cs" or event.get("is_diagnostic"):
            return False
        account = str(event.get("account") or "").strip()
        buyer = str(event.get("buyer_id") or "").strip()
        text = _OUTGOING_KEEP.sub("", str(event.get("content") or ""))
        if not account or not buyer or len(text) < 4:
            return False
        try:
            window = float(self.cfg.get("outgoing_dedup_seconds") or 60.0)
        except (TypeError, ValueError):
            window = 60.0
        now = time.time()
        recent = getattr(self, "_recent_outgoing", None)
        if recent is None:
            recent = self._recent_outgoing = []
        recent[:] = [item for item in recent if now - item["at"] <= window]
        score = len(text) + (100000 if str(event.get("platform_message_id") or "").isdigit() else 0)
        superseded = None
        for item in recent:
            if item["account"] != account or item["buyer"] != buyer:
                continue
            other = item["text"]
            if text == other or text.startswith(other) or other.startswith(text):
                if score > item["score"]:
                    superseded = item
                    break
                return True
        if superseded is not None:
            still_pending = any(
                str(e.get("event_id") or "") == superseded["event_id"] for e in self._pending)
            if not still_pending:
                # 更“完整”的那条已经上发出去了（比如 callback 已中心接受），晚到的平台回显
                # 不能再补一份 —— 直接把本条丢弃。
                return True
            self._pending = deque(
                e for e in self._pending
                if str(e.get("event_id") or "") != superseded["event_id"])
            self._pending_ids = {str(e.get("event_id") or "") for e in self._pending}
            recent.remove(superseded)
            self._outgoing_superseded = True
        recent.append({"at": now, "account": account, "buyer": buyer,
                       "text": text, "score": score,
                       "event_id": str(event.get("event_id") or "")})
        return False

    def _on_local_event(self, msg: dict) -> None:
        msg = stamp_message(msg)
        if msg.get("is_history"):
            ts = float(msg.get("ts") or 0)
            if ts <= 0:
                # 没有平台时间的历史帧永远上送不了（history_ready 恒为 False），
                # 会卡在待发队列里污染指标 —— 直接丢弃。
                return
            if str(msg.get("capture_source") or "").startswith("pdd_cdp"):
                # 只对 CDP 补拉回来的历史做收敛（探域日志回补保持原行为）。
                # CDP 补拉一次会带回十几条旧记录，全量上送只会被中心 ignored/拒收。
                try:
                    max_age = float(self.cfg.get("history_report_max_age_seconds") or 1800)
                except (TypeError, ValueError):
                    max_age = 1800
                user_only = str(self.cfg.get("history_report_user_only", "true")).strip().lower() \
                    not in {"0", "false", "no", "off", ""}
                if (max_age > 0 and (time.time() - ts) > max_age) or \
                        (user_only and str(msg.get("role") or "") != "user"):
                    return
        # 不串台：本机工作台只该看到本机登录的席位。判定放在 account 解析之后、
        # 记账之前 —— 被拦下的消息不该污染 _accounts_seen / _recent_buyers。
        # 生产版在 run_pdd_client.on_local_event 已先拦过一道，这里是精简版的兜底。
        if not self._seat_scope_allows(msg):
            self._note_out_of_scope(msg)
            return
        account = str(msg.get("account") or "").strip()
        if account:
            self._accounts_seen[account] = time.time()
        if account and msg.get("buyer_id") and not msg.get("is_history"):
            # 记录“最近活跃会话”（只看实时帧），供 CDP 周期补拉历史
            recent = getattr(self, "_recent_buyers", None)
            if recent is None:
                recent = self._recent_buyers = {}
            recent[(account, str(msg.get("buyer_id")))] = time.time()
        event = self._ensure_event_id({
            "type": "message",
            "platform": msg.get("platform") or self.platform.name,
            "idempotency_key": msg.get("idempotency_key") or "",
            "msg_id": msg.get("msg_id"),
            "platform_message_id": msg.get("platform_message_id") or "",
            "identity_kind": msg.get("identity_kind") or "",
            "buyer_id": msg.get("buyer_id"),
            "role": msg.get("role"),
            "content": msg.get("content"),
            "ts": msg.get("ts"),
            "platform_ts_key": msg.get("platform_ts_key") or "",
            "parent_msg_id": msg.get("pre_msg_id") or msg.get("parent_msg_id") or "",
            "account": account,
            "shop_id": msg.get("shop_id") or "",
            "shop_name": msg.get("shop_name") or msg.get("mall_name") or "",
            "buyer_nick": msg.get("buyer_nick") or msg.get("nickname") or "",
            "order_context": msg.get("order_context") or {},
            "order_id": msg.get("order_id") or "",
            "order_info": msg.get("order_info") or {},
            "local_context_lookup": msg.get("local_context_lookup") or {},
            "goods_id": msg.get("goods_id") or "",
            "goods_name": msg.get("goods_name") or "",
            "goods_url": msg.get("goods_url") or "",
            "goods_thumb_url": msg.get("goods_thumb_url") or "",
            "goods_price": msg.get("goods_price") or "",
            "goods_spec": msg.get("goods_spec") or "",
            "goods_context_source": msg.get("goods_context_source") or "",
            "template_name": msg.get("template_name") or "",
            "raw_type": msg.get("raw_type", msg.get("message_type")),
            "sent_by": self._classify_sent_by(msg),
            "source": msg.get("source"),
            "skipped_reason": msg.get("skipped_reason") or "",
            "is_diagnostic": bool(msg.get("is_diagnostic")),
            "frame_kind": msg.get("frame_kind") or "",
            "delivery_status": msg.get("delivery_status") or "",
            "agent_id": self.cfg["agent_id"],
            **{key: msg[key] for key in TIMING_FIELDS if key in msg},
            "enqueued_at": msg.get("enqueued_at") or time.time(),
        })
        if event.get("is_diagnostic"):
            # 诊断帧不进中心上传队列: 中心契约只收聊天消息, 上送必被拒成死信噪音。
            # 原始帧源头已 _archive_raw 归档, 台账照记 captured, 本地 127 工作台照推。
            self._ledger_note(event["event_id"], "captured", event=event,
                              detail="诊断帧, 不上送中心")
        else:
            drop_duplicate = False
            with self._pending_lock:
                if not hasattr(self, "_pending_ids"):
                    self._pending_ids = {e["event_id"] for e in self._pending}
                if event["event_id"] in self._pending_ids:
                    log.info("event duplicate skipped id=%s（已在本机待发队列里）", event["event_id"])
                    self._ledger_note(event["event_id"], "center_duplicate_skipped", event=event,
                                        detail="已在本机待发队列中")
                    return
                if self._cross_source_duplicate(event):
                    # 另一条腿已经报过这一条（平台消息 id 相同）。中心按 event_id
                    # 幂等，不会重复回复，但流量和日志都会翻倍。
                    self._ledger_note(event["event_id"], "cross_source_duplicate_skipped",
                                      event=event, detail="另一数据源已上报同一条")
                    return
                if self._merge_outgoing_duplicate(event):
                    drop_duplicate = True
                else:
                    self._append_local_queue(event)
                    self._pending.append(event)
                    self._pending_ids.add(event["event_id"])
                    self._remember_source_time(event)
            if drop_duplicate:
                self._ledger_note(event["event_id"], "outgoing_dedup_skipped", event=event,
                                  detail="同一次发送的重复记录(已有更完整的一条)")
                log.info("event outgoing duplicate skipped id=%s msg_id=%s",
                         event["event_id"], str(event.get("msg_id") or "")[:100])
                return
            if getattr(self, "_outgoing_superseded", False):
                self._outgoing_superseded = False
                self._rewrite_local_queue()
            self._ledger_note(event["event_id"], "captured", event=event)
            log.info("event queued id=%s msg_id=%s ts=%s captured_at_ms=%s enqueued_at=%s history=%s source=%s",
                     event["event_id"], str(event.get("msg_id") or "")[:100], event.get("ts"),
                     event.get("captured_at_ms"), event.get("enqueued_at"),
                     bool(event.get("is_history")), str(event.get("source") or "")[:40])
        if self._local_ingest_queue is not None:
            try:
                self._local_ingest_queue.put_nowait(event)
            except Exception:
                pass

    def _seat_scope_allows(self, event: dict) -> bool:
        """归属闸门（精简版 / 上传兜底共用）。

        用惰性访问器而不是直接 self._seat_scope：测试大量用
        `object.__new__(BridgeAgent)` 跳过 __init__，直接访问新属性会 AttributeError
        打挂整条链路（v0.9.0 / v0.9.2 / v0.9.4 各踩过一次）。
        """
        scope = getattr(self, "_seat_scope", None)
        if scope is None:
            try:
                scope = self._seat_scope = _build_seat_scope(self)
            except Exception as exc:
                log.debug("SeatScope 构造失败, 本批不做归属过滤: %s", exc)
                return True
        return scope.allows(event.get("account"), event.get("role")) != SCOPE_BLOCK

    def _note_out_of_scope(self, msg: dict) -> None:
        """精简版拦下消息时的留档 + 计数。

        生产版在 `run_pdd_client._archive_out_of_scope` 里有完整实现（写
        `*_out_of_scope.jsonl`）；这里只做计数与一条节流日志 —— 精简版没有
        生产版的本地队列路径，不重复实现一份。
        """
        counters = getattr(self, "_scope_out_of_scope", None)
        if counters is None:
            counters = self._scope_out_of_scope = {"count": 0, "last": ""}
        counters["count"] += 1
        counters["last"] = str(msg.get("account") or "")
        self._log_throttled(
            "out_of_scope",
            "消息不是本机席位，已拦下（account=%r）—— 本机席位见 status 的 seat_scope.seats",
            msg.get("account"))

    def _local_ingest_loop(self) -> None:
        """串行推送到本机 gateway 的 local-seat ingest 端点（daemon，不阻塞主链路）。"""
        url = self._local_ingest_url
        headers = {
            "X-Agent-Token": str(self.cfg.get("agent_token") or ""),
            "X-Agent-Id": str(self.cfg.get("agent_id") or ""),
            "Content-Type": "application/json; charset=utf-8",
        }
        while not self._stop.is_set():
            try:
                event = self._local_ingest_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                body = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    resp.read()
                self._ledger_note(self._ledger_event_id(event), "local_ok",
                                    event=event, source="ingest")
            except Exception as exc:
                log.debug("本地工作台推送失败(不影响主链路): %s", exc)
                self._ledger_note(self._ledger_event_id(event), "local_fail",
                                    event=event, source="ingest", detail=str(exc))

    def _ledger_center_acks(self, acknowledgements: list, sent_by_id: dict) -> None:
        """台账: 每条消息在中心侧的结果（接受/忽略/拒收/让重试）。"""
        for item in acknowledgements:
            if not isinstance(item, dict):
                continue
            ack_id = str(item.get("event_id") or "")
            if not ack_id:
                continue
            event = sent_by_id.get(ack_id) or {}
            status = str(item.get("status") or "")
            reason = str(
                item.get("reason")
                or (item.get("error") if isinstance(item.get("error"), dict) else {}).get("code")
                or status
            )
            if status == "accepted" or item.get("committed") is True:
                self._ledger_note(ack_id, "center_accepted", event=event)
            elif status == "ignored":
                self._ledger_note(ack_id, "center_ignored", event=event, detail=reason)
            elif status == "rejected":
                self._ledger_note(ack_id, "center_refused", event=event, detail=reason)
            if item.get("retryable"):
                self._ledger_note(ack_id, "center_retry", event=event, detail=reason)

    def _ledger_note(self, event_id: str, state: str, *, event: dict | None = None,
                     detail: str = "", source: str = "") -> None:
        """台账写入（没有台账的场合直接跳过, 绝不影响主链路）。"""
        ledger = getattr(self, "_ledger", None)
        if ledger is None or not event_id:
            return
        ledger.record(event_id, state, event=event, detail=detail, source=source)

    def _append_local_queue(self, event: dict) -> None:
        try:
            append_event(self.queue_path, event)
        except OSError as exc:
            self._last_error = f"append_local_queue: {exc}"
            log.warning("append local event queue failed: %s", type(exc).__name__)
            raise

    def _rewrite_local_queue(self) -> None:
        with self._pending_lock:
            pending = list(self._pending)
            try:
                self.queue_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.queue_path.with_suffix(self.queue_path.suffix + ".tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    for event in pending:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                # 高并发下 Windows 的 os.replace 会偶发 WinError 5（目标文件刚被另一
                # 线程打开）/32（被扫描器占用）。重试几次即可，不该当故障上报。
                for attempt in range(4):
                    try:
                        temporary.replace(self.queue_path)
                        break
                    except OSError as exc:
                        if attempt == 3:
                            # 队列文件只是“待发快照”：落盘失败最多导致重启后重复上报
                            # （中心按 msg_id 幂等），不会丢消息，因此只记 debug。
                            log.debug("rewrite local event queue failed: %s", exc)
                            break
                        time.sleep(0.02 * (attempt + 1))
            except OSError as exc:
                self._last_error = f"rewrite_local_queue: {exc}"
                log.warning("rewrite local event queue failed: %s", exc)

    def _status_payload(self) -> dict:
        # Keep heartbeat light: channel_status may touch netstat; catch all failures
        status_cfg = self.cfg
        if self.pddbridge_source is not None:
            # 让 channel_status_cdp 能看到注入状态, 报告真实的 dll_ready/injected
            status_cfg = dict(self.cfg)
            status_cfg["_pddbridge_source"] = self.pddbridge_source
        status_cfg = dict(status_cfg)
        status_cfg["_effective_source"] = self._effective_source
        try:
            ch = self.platform.channel_status(status_cfg) or {}
        except Exception as exc:
            ch = {"dll_ready": False, "hint": f"channel_status error: {exc}"}
        now = time.time()
        accounts = sorted(
            acc for acc, ts in self._accounts_seen.items()
            if now - ts < 3600
        )
        ob = {}
        # 中心 WS 上行通道的运行状态：连没连上、发了多少帧、收到多少 ack/result。
        # 放进心跳是为了避免"以为在跑 WS、其实一直在走 HTTP"这种静默降级。
        center_ws_status: dict = {}
        center_ws_channel = getattr(self, "center_ws", None)
        if center_ws_channel is not None:
            try:
                center_ws_status = center_ws_channel.status()
            except Exception as exc:  # noqa: BLE001
                center_ws_status = {"error": str(exc)}
        if self.platform.name == "taobao":
            try:
                from .openbot_ws import status_snapshot
                from .qn_inject import inject_status

                ob = {**status_snapshot(), "inject": inject_status()}
            except Exception as exc:
                ob = {"openbot_error": str(exc)}
        # Shop identity for brain-server merge (1 shop many CS accounts).
        shops: List[dict] = []
        seen_shop_acc: set = set()
        for acc in accounts:
            # Includes Taobao nicks (seller -> tb_nick_<seller>) so a shop can
            # be discovered before it has an explicit shop_map entry.
            sid = self._account_shop_id(acc)
            if not sid:
                continue
            key = (sid, acc)
            if key in seen_shop_acc:
                continue
            seen_shop_acc.add(key)
            shops.append({
                "shop_id": sid,
                "account": acc,
                "platform": self.platform.name,
                "source": "bridge_heartbeat",
            })
        # 活动数据源: cdp(PDD CDP 实时) / tanyu_logs(探域日志, 含自动降级)
        effective = self._effective_source
        cdp_port = ch.get("cdp_port")
        cdp_stats = None
        if self.pddbridge_source is not None:
            cdp_stats = self.pddbridge_source.status()
            if cdp_stats.get("port"):
                cdp_port = cdp_stats["port"]
        if effective == "cdp" and self.pddbridge_source is not None:
            attached = (
                {"pdd_cdp": "port %s injected" % cdp_stats["port"]}
                if cdp_stats.get("injected")
                else {"pdd_cdp": "scanning"}
            )
            watcher_stats = self.pddbridge_source.diagnostics()
        else:
            attached = dict(self.watcher.attached)
            watcher_stats = self.watcher.diagnostics()
        src_last_error = (
            self.pddbridge_source.last_error if self.pddbridge_source is not None else ""
        )
        return {
            "version": __version__,
            "build_hash": __build_hash__,
            "platform": self.platform.name,
            "platform_label": self.platform.label,
            "watching": self._active_watching(),
            "attached": attached,
            "tanyu_log_dir": self.cfg.get("tanyu_log_dir"),
            "data_source": self._data_source,
            "effective_source": effective,
            "accounts_seen": accounts,
            "shops": shops,
            "allowed_shop_ids": [],
            "client_shop_filtering": False,
            "scope_filtered_events": self._scope_filtered_events,
            "scope_blocked_commands": self._scope_blocked_commands,
            "pending_events": len(self._pending),
            "pending_command_acks": self.command_journal.pending_count(),
            "watcher_stats": watcher_stats,
            # CDP 源的完整状态（会话列表 / drops / pump 轮转计数）。10 店铺场景
            # 要判断"预算和轮询调得对不对"只能看这里 —— 尤其 pump.round_budget_hit。
            "pddbridge": cdp_stats,
            # 命令通道可见性：收没收到指令、轮询有没有在报错。原来这些只能靠翻日志。
            #   received=0 且 poll_fail 在涨  → 长轮询本身出错
            #   received=0 且 poll_ok 在涨   → 轮询正常但中心没派发（查中心侧）
            "commands": dict(self._cmd_counters()),
            "cross_source_dedup": dict(
                self._ingest_stats_dict(),
                window_seconds=round(
                    float(getattr(self, "_ingest_window", _INGEST_DEDUP_MIN_SECONDS)), 1),
                table_size=len(self._ingest_seen_table()),
            ),
            "dll_ready": ch.get("dll_ready") or bool(ob.get("openbot_connected")),
            "receive_ready": bool(ch.get("receive_ready", self.platform.name != "taobao")),
            "dll_port": ch.get("dll_port"),
            "cdp_port": cdp_port,
            "workbench_pid": ch.get("workbench_pid"),
            "port_discovery": ch.get("port_discovery"),
            "channel_hint": ch.get("hint") or "",
            "hint": (
                (
                    "openbot 桥已连接，可发送"
                    if ob.get("openbot_connected")
                    else ch.get("hint") or ""
                )
            ),
            "last_error": self._last_error or src_last_error or self.watcher.last_error,
            "dry_run": bool(self.cfg.get("dry_run")),
            "openbot": ob,
            "center_ws": center_ws_status,
        }

    def _allowlist_enforced(self) -> bool:
        return False

    def _active_watching(self) -> bool:
        """当前活动数据源的监听线程是否存活（CDP 或探域日志）。"""
        if self._effective_source == "cdp":
            src = getattr(self, "pddbridge_source", None)
            return bool(src and src._thread and src._thread.is_alive())
        return bool(self.watcher._thread and self.watcher._thread.is_alive())

    def _start_feeds(self) -> None:
        """只跑当前生效的数据源。

        以前是无条件两条通道同时上报，理由是「CDP 看着健康也可能漏一帧」。代价是同一个买家
        会被拆成多张会话卡片：日志里的账号是探域的展示名（主账号 / pdd42730237415），CDP 是
        cs_商城:席位，中心按 (account, buyer_id) 建会话，于是浮窗上同一个买家出现两三张。
        实测日志通道 5 小时里补到的买家消息是 0 条，只回放过我们自己发出的 mall_cs。
        CDP 不可用时仍由 _fallback_to_tanyu_logs 拉起日志通道（它有游标，漏掉的时间段会补读）。
        """
        if self._effective_source == "cdp" and self.pddbridge_source is not None:
            self.pddbridge_source.start()
            log.info("已开始 PDD CDP 实时监听（%s）", self.platform.label)
        else:
            self.watcher.start()  # 幂等
            log.info("已开始监听本机探域日志（%s）", self.platform.label)

    def _fallback_to_tanyu_logs(self) -> None:
        """CDP 源不可用时切回探域日志（PddbridgeSource 降级回调, 线程安全）。"""
        with self._pending_lock:
            if self._effective_source == "tanyu_logs":
                return
            self._effective_source = "tanyu_logs"
            self.watcher.start()  # 幂等
        self._last_error = "CDP 通道未就绪，已自动降级到探域日志数据源"
        log.warning("已自动降级到探域日志数据源（data_source=%s）", self._data_source)

    def _recover_to_cdp(self) -> None:
        """CDP 恢复后切回 CDP 数据源，停掉降级用的探域日志通道（PddbridgeSource 回调）。

        以前降级是单行道: PDD 工作台修好后桥接也永远停在日志通道，客户机必须重启/重装
        桥接才能恢复。现在降级中的 CDP 线程每 30s 重试，挂上就回调这里切回。
        """
        # tanyu_logs 为主 + CDP 周期补拉时，不让 CDP 的“恢复”回调把数据源切走
        try:
            pull_seconds = float(self.cfg.get("history_pull_seconds") or 0)
        except (TypeError, ValueError):
            pull_seconds = 0
        if pull_seconds > 0 and str(self.cfg.get("data_source") or "") == "tanyu_logs":
            return
        with self._pending_lock:
            if self._effective_source == "cdp":
                return
            self._effective_source = "cdp"
        try:
            self.watcher.stop()
        except Exception as exc:
            log.warning("停止探域日志通道失败（忽略）: %s", exc)
        self._last_error = ""
        log.info("CDP 已恢复，切回 CDP 数据源并停止探域日志通道")

    def _apply_registered_agent_id(self, registration: dict) -> bool:
        resolved = str((registration or {}).get("agent_id") or "").strip()
        if not resolved:
            return False
        previous = str(self.cfg.get("agent_id") or "").strip()
        self.cfg["agent_id"] = resolved
        self.client.agent_id = resolved
        # WS 握手要带 X-Agent-Id，身份以服务端解析结果为准，这里同步过去。
        channel = getattr(self, "center_ws", None)
        if channel is not None:
            channel.agent_id = resolved
        if resolved == previous:
            return False
        try:
            save_config(self.cfg)
        except Exception:
            pass
        return True

    @staticmethod
    def _ledger_event_id(event: dict) -> str:
        return str(event.get("event_id") or event.get("idempotency_key") or "")

    def _record_event_loss(self, batch: list[dict], losses: list[tuple[dict, str]]) -> None:
        """把中心拒收的事件追加到 *_refused.jsonl，供人工补推；同时记台账。"""
        reasons = {str(item.get("event_id") or ""): reason for item, reason in losses}
        path = self.queue_path.with_name(self.queue_path.stem + "_refused.jsonl")
        try:
            with path.open("a", encoding="utf-8") as handle:
                for event in batch:
                    event_id = str(event.get("event_id") or "")
                    if event_id in reasons:
                        handle.write(json.dumps(
                            {**event, "refused_reason": reasons[event_id]}, ensure_ascii=False) + "\n")
                        self._ledger_note(event_id, "dead_letter", event=event,
                                            detail=reasons[event_id])
                        # 这条从此不会再重发（本机侧），必须放开判重身份 ——
                        # 否则另一条腿带完整字段的救援副本会被永久挡住。
                        self._ingest_release(event)
        except OSError as exc:
            log.warning("record refused events failed: %s", exc)

    def _build_center_ws(self) -> Optional[CenterEventChannel]:
        """按配置建中心 WS 通道。默认关；任何异常都只记日志并返回 None（走 HTTP）。"""
        if not as_bool(self.cfg.get("center_ws_enabled"), False):
            return None
        try:
            # 显式配置优先（WS 走独立入口时用），否则从 server_url 推导。
            url = str(self.cfg.get("center_ws_url") or "").strip() or ws_url_from_server_url(
                self.cfg.get("server_url") or "",
                str(self.cfg.get("center_ws_path") or DEFAULT_WS_PATH),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("中心 WS 地址推导失败，改用 HTTP 上行: %s", exc)
            return None
        try:
            return CenterEventChannel(
                ws_url=url,
                token=str(self.cfg.get("agent_token") or ""),
                agent_id=str(self.cfg.get("agent_id") or ""),
                device_id=str(self.cfg.get("device_id") or ""),
                platform=self.platform.name,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("中心 WS 通道创建失败，改用 HTTP 上行: %s", exc)
            return None

    def _upload_batch(self, batch: List[dict]) -> dict:
        """优先走中心 WS，不可用时回落 HTTP。

        两条路径共用服务端同一张落表与同一 event_id 唯一约束，所以即使在
        `send_events` 已经把部分帧发出去之后才回落，重发也不会双投。
        """
        channel = getattr(self, "center_ws", None)
        if channel is not None and channel.available:
            try:
                timeout = float(self.cfg.get("center_ws_ack_timeout_seconds") or 10.0)
            except (TypeError, ValueError):
                timeout = 10.0
            try:
                # 测试/模拟里 agent 常用 object.__new__ 绕过 __init__，这两个属性
                # 可能不存在；缺失就跳过留痕，不影响上行本身。
                lock = getattr(self, "_ws_recent_lock", None)
                recent = getattr(self, "_ws_recent", None)
                if lock is not None and recent is not None:
                    with lock:
                        for event in batch:
                            event_id = str(event.get("event_id") or "")
                            if event_id:
                                recent[event_id] = event
                        while len(recent) > _WS_RECENT_LIMIT:
                            recent.popitem(last=False)
                return channel.send_events(batch, timeout=timeout)
            except CenterWsUnavailable as exc:
                self._last_error = f"center_ws 回落 HTTP: {exc}"
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"center_ws 异常回落 HTTP: {exc}"
                log.warning("中心 WS 上行异常，本批回落 HTTP: %s", exc)
        return self.client.upload_events(batch)

    def _drain_center_ws_results(self) -> None:
        """消费业务终态 result：processed/retry 记账，rejected 落死信。

        result 服务端**不补投**，所以断线期间的结果会丢；这里只处理在线期间收到的。
        出队不依赖 result（ack 已表示落表），所以丢 result 不会丢消息。
        """
        channel = getattr(self, "center_ws", None)
        if channel is None:
            return
        rows = channel.drain_results()
        if not rows:
            return
        lock = getattr(self, "_ws_recent_lock", None)
        recent = getattr(self, "_ws_recent", None)
        for row in rows:
            event_id = str(row.get("event_id") or "")
            status = str(row.get("status") or "")
            reason = str(row.get("error_code") or status or "")
            event = None
            if lock is not None and recent is not None:
                with lock:
                    event = recent.pop(event_id, None)
            event = event or {"event_id": event_id}
            if status == "rejected":
                self._ledger_note(event_id, "center_refused", event=event, detail=reason)
                # 复用 HTTP 路径同一套死信文件，人工可补推。
                self._record_event_loss([event], [(event, f"result:{reason}")])
            elif status == "processed":
                self._ledger_note(event_id, "center_processed", event=event)
            else:
                # retry：服务端自己退避重试（≤8 次），客户端不需要动作。
                self._ledger_note(event_id, "center_retry", event=event, detail=reason)

    def _upload_batch_size(self) -> int:
        """每批上送中心的条数。突发进线时批越小、批数越多，而每批都要等一次本地优先
        门控（~1s），尾部就迟到得越久；可用 upload_batch_size 调大（上限 2000）。"""
        try:
            value = int(self.cfg.get("upload_batch_size") or 0)
        except (TypeError, ValueError):
            value = 0
        return max(1, min(value or 500, 2000))

    def _flush_events(self, *, blocked_ids: Optional[set[str]] = None) -> None:
        now = time.time()
        history_now = self.client.server_now() - 1.0 if hasattr(self.client, "server_now") else now
        if isinstance(self.client, BridgeClient) and self.client._server_clock is None:
            history_now = 0.0
        if time.monotonic() < getattr(self, "_upload_retry_at", 0):
            return
        with self._pending_lock:
            inflight = getattr(self, "_inflight_ids", None)
            if inflight is None:
                inflight = self._inflight_ids = set()
            for event in self._pending:
                event.update(stamp_message(event, now=now))
            ready = [e for e in self._pending
                     # 兜底：_load_local_queue / replace_pending_event 会把旧事件直接
                     # 塞进 _pending，绕过 _on_local_event 的归属闸门 —— 这里再挡一道，
                     # 否则重启后历史串台事件照样上报中心。
                     if self._seat_scope_allows(e)
                     if str(e.get("event_id") or "") not in inflight
                     and e.get("event_id") not in (blocked_ids or set())
                     and history_ready(e, history_now if e.get("is_history") else now)]
            # Live traffic goes first; reserve part of each batch for acknowledged backfill.
            live = [e for e in ready if not e.get("is_history")]
            history = [e for e in ready if e.get("is_history")]
            batch_size = self._upload_batch_size()
            live_quota = min(len(live), max(1, int(batch_size * 4 / 5)))
            batch = live[:live_quota] + history[:max(1, batch_size - live_quota)]
            if not history:
                batch = live[:batch_size]
            if not batch:
                return
            # 标记在途：多路上传并发时，各批互不重叠
            inflight.update(str(e.get("event_id") or "") for e in batch)
        log.info("event upload batch=%d oldest_wait=%.3fs ids=%s", len(batch),
                 queue_metrics(batch)["oldest_wait_seconds"], [e.get("event_id") for e in batch])
        queue_changed = False
        terminal_ids = set()
        try:
            response = self._upload_batch(batch)
            self._upload_retry_delay = 0.5
            with self._pending_lock:
                ack_version = (response or {}).get("ack_version")
                if isinstance(ack_version, int) and ack_version >= 1:
                    acknowledgements = (
                        response.get("event_acks") if isinstance(response.get("event_acks"), list) else []
                    )
                    terminal_ids = {
                        str(item.get("event_id") or "")
                        for item in acknowledgements
                        if isinstance(item, dict)
                        and (item.get("committed") is True or item.get("retryable") is False)
                    }
                    # ack 里只有中心知道的字段; 要用我们自己事件上的标记(is_diagnostic 等)
                    # 判断“这算不算客户端故障”, 所以先把 ack 对回原事件。
                    sent_by_id = {
                        str(event.get("event_id") or event.get("idempotency_key") or ""): event
                        for event in batch if isinstance(event, dict)
                    }
                    refused = [
                        (
                            sent_by_id.get(str(item.get("event_id") or "")) or {},
                            str(
                                item.get("reason")
                                or (item.get("error") if isinstance(item.get("error"), dict) else {}).get("code")
                                or item.get("status")
                                or ""
                            ),
                        )
                        for item in acknowledgements
                        if isinstance(item, dict)
                        and str(item.get("status") or "") in {"ignored", "rejected"}
                    ]
                    self._ledger_center_acks(acknowledgements, sent_by_id)
                    # 被拒收的一律放开判重占用（含后面会被 losses 过滤掉的
                    # plugin_send_echo）—— 它们同样没送达中心。
                    for _refused_event, _refused_reason in refused:
                        self._ingest_release(_refused_event)
                    # 只按 event_id 就地移除“本批且已终态”的事件。原来的 clear+extend 依赖
                    # 更早的快照，多路并发上传时会互相覆盖（丢事件）。
                    batch_event_ids = {
                        str(event.get("event_id") or event.get("idempotency_key") or "")
                        for event in batch
                    }
                    before_count = len(self._pending)
                    self._pending = deque(
                        item for item in self._pending
                        if not (str(item.get("event_id") or item.get("idempotency_key") or "") in terminal_ids
                                and str(item.get("event_id") or item.get("idempotency_key") or "") in batch_event_ids)
                    )
                    queue_changed = len(self._pending) != before_count
                    retryable = [
                        item for item in acknowledgements
                        if isinstance(item, dict) and item.get("retryable")
                    ]
                    if retryable:
                        self._last_error = f"upload_events: {len(retryable)} event(s) awaiting retry"
                    # 中心明确拒收（unsupported_role / invalid_buyer ...）的事件过去被静默剔出
                    # 队列，本机却已推送给本地工作台 —— 也就是"本地有、大脑没有"。
                    # 这里落一份死信文件并写进 last_error，保证不再无声消失。
                    losses = [(item, reason) for item, reason in refused if reason != "plugin_send_echo"]
                    if losses:
                        self._record_event_loss(batch, losses)
                        reasons = sorted({reason for _, reason in losses})
                        # 以下是「中心/大脑的决定」而不是客户端故障, 不该弹到界面上:
                        #   ignored        = 大脑选择忽略(例如历史消息)
                        #   invalid_message= 中心内容过滤(例如“一长串数字”被当 uid 噪声)
                        #   unsupported_role + is_diagnostic = 中心只接买家消息, 我们的诊断帧本来就不在它契约内
                        # 死信文件和本地原始帧归档照记, 保证“没上报”不等于“没发生过”。
                        center_decisions = {"invalid_message", "ignored"}
                        if any(not item.get("is_diagnostic") and reason not in center_decisions
                               for item, reason in losses):
                            self._last_error = "upload_events: 中心拒收 %d 条：%s" % (
                                len(losses), ", ".join(reasons))
                        log.warning(
                            "event upload refused count=%d reasons=%s ids=%s",
                            len(losses), reasons,
                            [str(item.get("event_id") or "") for item, _ in losses][:10],
                        )
                else:
                    self._last_error = "upload_events: explicit per-event acknowledgement required"
                    self._upload_retry_at = time.monotonic() + 5.0
                attempted = {id(e) for e in batch}
                before_rot = list(self._pending)
                waiting = [e for e in before_rot if id(e) in attempted]
                others = [e for e in before_rot if id(e) not in attempted]
                self._pending = deque(others + waiting)
                if self._pending != before_rot:
                    queue_changed = True
                if waiting and not others:
                    self._upload_retry_at = time.monotonic() + 5.0
                self._pending_ids = {str(e.get("event_id") or "") for e in self._pending}
            if queue_changed:
                self._rewrite_local_queue()
            terminal_count = sum(
                1 for e in batch if str(e.get("event_id") or "") in terminal_ids)
            log.info("event upload terminal=%d remaining=%d terminal_ids=%s",
                     terminal_count, len(self._pending),
                     [e["event_id"] for e in batch if e["event_id"] in terminal_ids])
        except BridgeClientError as exc:
            delay = min(5.0, getattr(self, "_upload_retry_delay", 0.5))
            self._upload_retry_at = time.monotonic() + delay
            self._upload_retry_delay = delay * 2
            self._last_error = f"upload_events: {exc}"
            log.warning("upload_events failed: %s", exc)
        finally:
            with self._pending_lock:
                self._inflight_ids -= {str(e.get("event_id") or "") for e in batch}

    # ------------------------------------------------------------------ 出站并发池
    # 背景（0.7.5 r2 的 P0）：一条回复要等平台回执，实测最长 10s，所以单线程出站的
    # 上限只有约 6 条/分钟/工位，而峰值需要约 38 条/分钟/工位。把「取指令」与「发送」
    # 解耦，取指令循环就不再被上一条的发送确认超时占住。
    #
    # 开关：**owner 2026-09-21 明确决定「默认全开」**（不是仓库默认的 fail-closed
    # 白名单）。判定顺序固定为：
    #   1) command_sender_enabled = false               -> 全关（回到改动前行为）
    #   2) 店铺在 command_sender_disabled_shop_ids 里    -> 只关这个店
    #   3) command_sender_shop_ids 非空                  -> 只有命中的店铺开（窄灰度）
    #   4) 否则                                         -> 开
    def _ensure_sender_state(self) -> None:
        """惰性初始化发送池状态（兼容绕过 __init__ 的构造路径）。"""
        if getattr(self, "_sender_queue", None) is None:
            self._sender_queue = queue.Queue(maxsize=64)
        if not hasattr(self, "_sender_threads"):
            self._sender_threads = []
        if not hasattr(self, "_sender_conv_locks"):
            self._sender_conv_locks = {}
        if not hasattr(self, "_sender_conv_locks_guard"):
            self._sender_conv_locks_guard = threading.Lock()
        if not hasattr(self, "_sender_pool_guard"):
            self._sender_pool_guard = threading.Lock()

    def _command_sender_shop_ids(self) -> List[str]:
        allowlist = self.cfg.get("command_sender_shop_ids")
        if not isinstance(allowlist, (list, tuple, set)):
            return []
        return [str(item or "").strip() for item in allowlist if str(item or "").strip()]

    @staticmethod
    def _shop_in_list(value: Any, shop_id: str) -> bool:
        if not shop_id or not isinstance(value, (list, tuple, set)):
            return False
        allowed = {str(item or "").strip() for item in value if str(item or "").strip()}
        return shop_id in allowed

    def _command_sender_on(self) -> bool:
        """总开关（默认开）。false 时整条并发发送路径完全不启用。"""
        return bool(self.cfg.get("command_sender_enabled", True))

    def _command_sender_enabled(self, account: Any) -> bool:
        """这个店铺的出站发送是否交给并发发送池（默认开，可总关 / 单店关 / 窄白名单）。"""
        if not self._command_sender_on():
            return False
        shop_id = str(self._account_shop_id(account) or "").strip()
        if self._shop_in_list(self.cfg.get("command_sender_disabled_shop_ids"), shop_id):
            return False
        allowlist = self._command_sender_shop_ids()
        if allowlist:
            return self._shop_in_list(allowlist, shop_id)
        return True

    def _sender_conversation_lock(self, account: Any, buyer_id: Any) -> threading.Lock:
        """同一买家保持串行，避免并行发送打乱回复顺序。"""
        self._ensure_sender_state()
        key = f"{str(account or '').strip()}|{str(buyer_id or '').strip()}"
        with self._sender_conv_locks_guard:
            lock = self._sender_conv_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._sender_conv_locks[key] = lock
            return lock

    def _sender_worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                cmd = self._sender_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                account = str(cmd.get("account") or "").strip()
                buyer_id = str(cmd.get("buyer_id") or "").strip()
                with self._sender_conversation_lock(account, buyer_id):
                    self._execute_command(cmd)
            except Exception:
                # 单条命令异常不能打死发送线程，否则出站会整体停摆。
                log.exception("command sender worker failed")
            finally:
                self._sender_queue.task_done()

    def _start_sender_pool(self) -> None:
        """启动并发发送池。幂等、线程安全；总开关关掉时一个线程都不建。"""
        self._ensure_sender_state()
        if not self._command_sender_on():
            return
        with self._sender_pool_guard:
            if self._sender_threads:
                return
            try:
                workers = int(self.cfg.get("command_sender_workers") or 6)
            except (TypeError, ValueError):
                workers = 6
            workers = max(1, min(workers, 8))
            for index in range(workers):
                thread = threading.Thread(
                    target=self._sender_worker_loop,
                    name=f"bridge-command-sender-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._sender_threads.append(thread)
            log.info("出站并发发送池已启动：%d 个 worker，范围=%s",
                     workers,
                     ("窄白名单 " + ", ".join(self._command_sender_shop_ids()))
                     if self._command_sender_shop_ids() else "全部店铺（默认全开）")

    def _apply_session_state(self, cmd: dict) -> dict:
        """把中心下发的会话状态（转人工 / AI 开关）落地到本机工作台。

        之前 session_state 被当作“不支持的命令类型”丢弃，导致中心已经转人工、
        本机浮窗/127 工作台却仍显示 AI 接待。
        """
        base = str(self.cfg.get("local_workbench_url") or "").rstrip("/")
        if not base:
            return {"ok": False, "status": "unsupported", "error": "local_workbench_url 未配置"}
        payload = {k: v for k, v in cmd.items() if k != "id"}
        meta = cmd.get("meta") if isinstance(cmd.get("meta"), dict) else {}
        for key in ("handoff", "handoff_reason", "ai_takeover_enabled", "ai_takeover_state",
                    "shop_ai_takeover_enabled", "reason", "account", "buyer_id"):
            if key not in payload and key in meta:
                payload[key] = meta[key]
        try:
            req = urllib.request.Request(
                base + "/api/local-seat/v1/session-state",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "X-Agent-Token": str(self.cfg.get("agent_token") or ""),
                    "X-Agent-Id": str(self.cfg.get("agent_id") or ""),
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read()
            return {"ok": True, "status": "applied", "real_send": False, "via": "local_seat"}
        except Exception as exc:
            log.warning("apply session_state locally failed: %s", exc)
            return {"ok": False, "status": "failed", "real_send": False, "error": str(exc)}

    def _cmd_counters(self) -> Dict[str, Any]:
        """命令通道计数表（惰性创建）。

        用 getattr 兜底而不是直接 self._cmd_stats：测试里大量用
        `object.__new__(BridgeAgent)` 造 agent（跳过 __init__），直接访问属性会
        AttributeError 把整条命令处理打挂 —— v0.9.0 接 WS 时同一个坑已经踩过一次。
        """
        counters = getattr(self, "_cmd_stats", None)
        if counters is None:
            counters = {"poll_ok": 0, "poll_fail": 0, "received": 0, "sent_ok": 0,
                        "no_id": 0, "last_received_at": 0.0, "last_error": ""}
            self._cmd_stats = counters
        return counters

    def _ingest_seen_table(self) -> Dict[str, float]:
        """跨数据源判重表（惰性创建）。

        与 _cmd_counters 同理：测试大量用 `object.__new__(BridgeAgent)` 造 agent
        （跳过 __init__），直接访问属性会 AttributeError 打挂整条消息处理。
        """
        table = getattr(self, "_ingest_seen", None)
        if table is None:
            table = self._ingest_seen = {}
        return table

    def _ingest_stats_dict(self) -> Dict[str, Any]:
        """判重计数表（同样惰性创建）。"""
        stats = getattr(self, "_ingest_stats", None)
        if stats is None:
            stats = self._ingest_stats = {"dropped": 0, "last_drop_at": 0.0}
        return stats

    @staticmethod
    def _ingest_identity(event: dict) -> str:
        """跨数据源判重用的身份 —— 平台消息 id。

        只认平台给的 id。合成的兜底 id（内容+时间哈希、callback-/frame-/imws- 前缀）
        可能撞哈希，拿它判重会误杀真实消息（同 pddbridge_source._is_fallback_id 的
        理由）。认不出来就返回 ""，调用方直接放行 —— 宁可重复也不丢。
        """
        if event.get("is_diagnostic"):
            return ""
        for key in ("platform_message_id", "msg_id"):
            identity = str(event.get(key) or "").strip()
            if not identity:
                continue
            low = identity.lower()
            if low.startswith(("callback-", "frame-", "imws-")):
                continue
            if len(identity) == 24 and all(ch in "0123456789abcdef" for ch in low):
                continue
            return identity
        return ""

    def _ingest_release(self, event: dict) -> None:
        """放开一条事件占用的跨源判重身份。

        **必须有**：判重表记的是"这条已经被另一条腿报过了"，但那只在**那次上报真的
        送出去了**的前提下才成立。

        实测踩的坑（2026-09-21 00:31 报、00:36 被拦）：探域日志腿上报的副本 account
        为空 → 中心拒收 → 落死信（**从没到达中心**）；5 分钟后 CDP 腿带着完整 account
        重新上报同一条，却被判重当"重复"挡在门外 —— **把"延迟 5 分钟送达"变成了
        "永久丢失"**。这在 v0.9.3（加判重之前）是能救回来的。

        所以：只要一条事件最终**没送达中心**（拒收/死信），就必须把它占的身份还回去，
        另一条腿的救援副本才有机会进来。
        """
        identity = self._ingest_identity(event)
        if identity:
            self._ingest_seen_table().pop(identity, None)

    def _prune_ingest_seen(self, now: float) -> None:
        """清掉已经出窗口的条目。判据是 `now - last < 窗口`，窗口外的条目再也不可能
        命中，清掉不改变任何判重决策；不清的话 2-3 条/秒跑满一天班就是几十万条。"""
        table = self._ingest_seen_table()
        if len(table) <= _INGEST_DEDUP_PRUNE_THRESHOLD:
            return
        if now - getattr(self, "_ingest_pruned_at", 0.0) < _INGEST_DEDUP_PRUNE_INTERVAL_SECONDS:
            return
        self._ingest_pruned_at = now
        cutoff = now - float(getattr(self, "_ingest_window", _INGEST_DEDUP_MIN_SECONDS))
        for key, last in list(table.items()):
            if last < cutoff:
                table.pop(key, None)

    def _cross_source_duplicate(self, event: dict) -> bool:
        """True = 本条已被**另一个**数据源报过，应丢弃。

        探域日志 watcher 与 CDP 周期回拉是两条独立管线，各自判重、互不知情 ——
        同一条买家消息会被两边各报一次（实测间隔 15~18s，台账里同一个 event_id
        出现两条 captured）。本方法在两条腿唯一的汇合点按平台消息 id 判重。

        命中时**刷新**时间戳（同 PddbridgeSource._seen_before）：回拉会按固定节奏
        反复重报同一条，不刷新的话累计到窗口之外就又漏一条，形成锯齿。
        """
        if str(event.get("role") or "") == "mall_cs":
            # 出站有自己的合并逻辑（_merge_outgoing_duplicate：按内容前缀合并、
            # 保留"更完整"的一条），它比"先到先得"更懂该留哪条。这里让路。
            return False
        identity = self._ingest_identity(event)
        if not identity:
            return False
        now = time.time()
        window = float(getattr(self, "_ingest_window", _INGEST_DEDUP_MIN_SECONDS))
        table = self._ingest_seen_table()
        last = table.get(identity)
        if last is not None and now - last < window:
            table[identity] = now
            stats = self._ingest_stats_dict()
            stats["dropped"] += 1
            stats["last_drop_at"] = now
            log.info("event 跨源重复已拦 id=%s identity=%s（另一数据源 %.1fs 前已上报）",
                     event.get("event_id"), identity, now - last)
            return True
        table[identity] = now
        self._prune_ingest_seen(now)
        return False

    def _log_throttled(self, key: str, message: str, *args, interval: float = 60.0) -> None:
        seen = getattr(self, "_cmd_warn_at", None)
        if seen is None:
            seen = self._cmd_warn_at = {}
        now = time.time()
        if now - seen.get(key, 0.0) >= interval:
            seen[key] = now
            log.warning(message, *args)

    def _claim_command(self, command_id: str) -> bool:
        """原子认领一条指令。

        **并发轮询的安全前提**：多条轮询可能同时拿到同一条指令，不认领就会
        **把同一条回复发给买家两次**。先到先得，后来的直接跳过。
        """
        lock = getattr(self, "_command_claim_lock", None)
        if lock is None:
            # 双检锁：**惰性建锁本身必须线程安全**。直接
            # `lock = self._command_claim_lock = threading.Lock()` 会有竞态 ——
            # N 个线程同时看见 None，各自建一把，互相不互斥，同一条指令能被
            # 认领多次（买家收到两条一样的回复）。用类级锁把初始化串起来。
            with BridgeAgent._claim_init_lock:
                lock = getattr(self, "_command_claim_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self._command_claim_lock = lock
                    self._command_claimed = set()
        with lock:
            claimed = self._command_claimed
            if command_id in claimed or command_id in self._commands_done:
                return False
            claimed.add(command_id)
            if len(claimed) > 2000:          # 有界，别无限涨
                self._command_claimed = set(list(claimed)[-1000:])
            return True

    def _command_poll_worker(self, wait: float) -> None:
        """并发的取指令线程（只取不补报，补报由主线程负责）。"""
        while not self._stop.is_set():
            try:
                self._handle_commands(wait_seconds=wait)
            except Exception:
                # 单条线程异常不能杀死整个取指令通道
                log.exception("命令处理异常（并发轮询线程继续）")
            self._stop.wait(0.2)

    def _handle_commands(self, *, wait_seconds: float = 0.0) -> None:
        counters = self._cmd_counters()
        # **取指令是时延关键路径，必须排在补报结果之前**。原来第一行是
        # `_retry_command_results()`，它每次一条 HTTP、超时 30 秒 —— 一挂住就把
        # 这一轮的取指令一起推迟，表现为"中心创建指令到桥接收到差 9 秒"。
        poll_started = time.monotonic()
        try:
            commands = self.client.pull_commands(wait_seconds=wait_seconds)
        except BridgeClientError as exc:
            # 长轮询失败原来是**完全静默**的：只写 _last_error，日志里一个字都没有。
            # 于是"中心没派发"和"轮询在报错"看起来一模一样（实测因此查了很久）。
            self._last_error = f"pull_commands: {exc}"
            counters["poll_fail"] += 1
            counters["last_error"] = f"pull_commands: {exc}"
            self._log_throttled(
                "pull_commands",
                "命令长轮询失败（累计 %d 次，收不到指令就是这个原因）: %s",
                counters["poll_fail"], exc,
            )
            self._retry_command_results()      # 轮询失败也要补报，别漏一轮
            return
        poll_elapsed = time.monotonic() - poll_started
        # 分段计时：周期被拖长时，日志里能看出是"取指令慢"还是"补报慢"。
        retry_started = time.monotonic()
        if not getattr(self, "_skip_result_retry", False):
            self._retry_command_results()
        retry_elapsed = time.monotonic() - retry_started
        if poll_elapsed > wait_seconds + 2.0 or retry_elapsed > 1.0:
            self._log_throttled(
                "poll_breakdown",
                "指令周期分段：取指令 %.1fs（预期约 %.1fs）+ 补报结果 %.1fs",
                poll_elapsed, wait_seconds, retry_elapsed, interval=30.0)
        counters["poll_ok"] += 1
        # 灰度店铺：投入并发发送池后立刻回去拉下一条，不再被上一条的发送确认
        # 超时（默认 10s）占住拉取循环；池子写满时退回串行执行形成背压，
        # 避免租约过期；未命中白名单（默认）完全走原来的串行路径。
        self._ensure_sender_state()
        inline: List[dict] = []
        for cmd in commands:
            account = str((cmd or {}).get("account") or "").strip()
            if self._command_sender_enabled(account):
                try:
                    self._sender_queue.put_nowait(cmd)
                    continue
                except queue.Full:
                    log.warning("command sender pool saturated; executing inline: %s",
                                str((cmd or {}).get("id")
                                    or (cmd or {}).get("command_id") or ""))
            inline.append(cmd)
        for cmd in inline:
            self._execute_command(cmd)

    def _execute_command(self, cmd: dict) -> None:
        """执行单条指令：认领 → 执行 → 存台账 → 回执。

        原来是 `_handle_commands` 里 `for cmd in commands:` 的循环体，为了让出站
        发送池能并发调用而原样抽出来（行为不变）。
        """
        counters = self._cmd_counters()
        command_id = str(cmd.get("id") or cmd.get("command_id") or "").strip()
        if not command_id:
            # 缺 id 的指令原来是直接 continue、一个字都不记。中心换了字段名
            # 就会变成"所有指令静默消失"，而日志上完全看不出来。
            counters["no_id"] += 1
            self._log_throttled(
                "no_command_id",
                "收到没有 id/command_id 的指令（累计 %d 条），已丢弃。"
                "中心侧字段名可能变了，指令内容: %s",
                counters["no_id"],
                json.dumps(cmd, ensure_ascii=False)[:300],
            )
            return
        if not self._claim_command(command_id):
            # 并发的另一条轮询已经拿到它了（或本机已处理过）——
            # 绝不能执行第二遍，否则买家会收到两条一样的回复。
            return
        counters["received"] += 1
        counters["last_received_at"] = time.time()
        existing = self.command_journal.get(command_id)
        if existing is not None:
            if existing.get("state") == "result_pending":
                self._report_command_result(command_id, dict(existing.get("result") or {}))
            return
        if command_id in self._commands_done:
            return
        buyer_id = str(cmd.get("buyer_id") or "").strip()
        account = str(cmd.get("account") or "").strip()
        content = str(cmd.get("content") or "").strip()
        cmd_type = str(cmd.get("type") or "send_text").strip() or "send_text"
        meta = cmd.get("meta") if isinstance(cmd.get("meta"), dict) else {}
        buyer_nick = str(meta.get("buyer_nick") or meta.get("nick") or "").strip()
        log.info(
            "command %s type=%s content_chars=%d",
            command_id, cmd_type, len(content),
        )
        if not self.command_journal.start(cmd):
            self._last_error = self.command_journal.last_error
            log.error("command journal unavailable; refusing execution: %s", command_id)
            return
        if cmd_type not in {"send_text", "open_chat", "pull_history", "session_state"}:
            result = {"ok": False, "status": "blocked", "real_send": False,
                      "retryable": False, "error": "unsupported_command_type"}
        elif cmd_type == "session_state":
            log.info("command %s session_state payload=%s", command_id,
                     json.dumps({k: v for k, v in cmd.items() if k != "id"},
                                ensure_ascii=False)[:500])
            result = self._apply_session_state(cmd)
        elif self._command_expired(cmd):
            result = {"ok": False, "status": "expired", "real_send": False,
                      "retryable": False, "via": "expired",
                      "error": "automatic_command_expired_or_unverifiable_time"}
        elif cmd_type == "pull_history":
            # 直拉历史：按买家 uid 分页取，不抢界面焦点（应答走既有 list 帧流上报）
            src = self.pddbridge_source
            if src is None:
                result = {"ok": False, "status": "unsupported", "real_send": False,
                          "error": "pddbridge source 未启动",
                          "error_user": "当前不是 PDD 直连模式，无法拉历史"}
            else:
                want = cmd.get("size") or cmd.get("count") or cmd.get("limit")
                try:
                    want_n = int(want) if want else None
                except (TypeError, ValueError):
                    want_n = None
                # 必须和上面的 size 一样兜住脏值：中心发 "0x10" 之类会让 int()
                # 抛异常，而 _handle_commands 整体没有隔离，异常会杀死命令
                # 长轮询线程 —— 心跳照常、界面显示运行中，但再也不执行任何指令。
                try:
                    start_index = int(cmd.get("start_index") or 0)
                except (TypeError, ValueError):
                    start_index = 0
                result = src.pull_history(
                    buyer_id, account, size=want_n,
                    begin_msg_id=cmd.get("begin_msg_id") or 0,
                    start_index=start_index,
                    pre_msg_id=cmd.get("pre_msg_id") or 0,
                ) or {}
        elif cmd_type == "open_chat":
            # Production adsorb jump: only focus conversation, no text send.
            try:
                open_fn = getattr(self.platform, "open_chat", None)
                if callable(open_fn):
                    open_cfg = dict(self.cfg)
                    open_cfg["_effective_source"] = self._effective_source
                    if self.pddbridge_source is not None:
                        open_cfg["_pddbridge_source"] = self.pddbridge_source
                    result = open_fn(buyer_id, account, buyer_nick=buyer_nick, cfg=open_cfg) or {}
                else:
                    # Fallback: attempt send_text empty is wrong; return guided failure
                    result = {
                        "ok": False,
                        "status": "unsupported",
                        "error": "platform has no open_chat",
                        "error_user": "当前平台桥接暂不支持一键跳转会话，请在官方客户端手动打开",
                        "real_send": False,
                        "via": "open_chat",
                    }
                if not isinstance(result, dict):
                    result = {"ok": bool(result), "via": "open_chat"}
                result.setdefault("via", "open_chat")
                result.setdefault("real_send", False)
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "error",
                    "error": str(exc),
                    "error_user": f"跳转会话失败：{exc}",
                    "real_send": False,
                    "via": "open_chat",
                }
        # refuse pure ??? — usually encoding death, not a real reply
        elif content and content.replace("?", "").strip() == "" and set(content) <= {"?"}:
            result = {
                "ok": False,
                "status": "blocked",
                "error": "content is only question marks",
                "error_user": "发送内容异常（只有 ???），已拦截；请重新输入中文再发",
                "real_send": False,
                "via": "blocked",
            }
        else:
            # 先登记“本桥接（AI）要发的这条”：探域日志常常在 send_text 返回之前
            # 就已经回显了这条消息，等发送结果回来再登记就晚了（人工会被当成智能体）。
            self._note_self_send(account, buyer_id, content)
            cfg = dict(self.cfg)
            if buyer_nick:
                cfg["_buyer_nick"] = buyer_nick
            if self.pddbridge_source is not None:
                cfg["_pddbridge_source"] = self.pddbridge_source
            # 分发按「活动数据源」而非配置意图: 自动降级后收发都走探域 DLL
            cfg["_effective_source"] = self._effective_source
            cfg["_command_is_expired"] = lambda cmd=cmd: self._command_expired(cmd)
            started = time.monotonic()
            try:
                result = self.platform.send_text(
                    buyer_id,
                    content,
                    account,
                    cfg=cfg,
                    dry_run=bool(self.cfg.get("dry_run")),
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "indeterminate",
                    "error": str(exc),
                    "error_user": "桥接发送过程异常；为避免重复发送，请人工核对会话",
                    "real_send": None,
                    "via": "send_exception",
                }
            # 出站默认由并发发送池执行（command_sender_enabled，默认开）；只有
            # enabled=false 或该店铺被 disabled_shop_ids 排除时才回到单线程串行。
            # 单线程时这条耗时直接决定“AI 回复吞吐”上限：容量 ≈ 1 / 平均耗时。
            # 实测日志里一半以上的指令→回显超过 10s，需要区分是“探域 DLL 慢”
            # 还是“发送确认没匹配上干等到超时”。
            duration = time.monotonic() - started
            if isinstance(result, dict) and result.get("real_send") is True:
                self._cmd_counters()["sent_ok"] += 1
            log.info("command %s send done in %.2fs status=%s real_send=%s via=%s",
                     command_id, duration,
                     str((result or {}).get("status") or "") if isinstance(result, dict) else "",
                     (result or {}).get("real_send") if isinstance(result, dict) else None,
                     str((result or {}).get("via") or "") if isinstance(result, dict) else "")
        if not isinstance(result, dict):
            result = {"ok": bool(result), "raw_result": str(result or "")}
        else:
            result = dict(result)
        result = _command_result_for_ack(result)
        result.setdefault("command_lease_token", str(cmd.get("lease_token") or ""))
        if not self.command_journal.store_result(command_id, result):
            self._last_error = self.command_journal.last_error
        self._report_command_result(command_id, result)

    def _self_send_buffer(self) -> Deque[dict]:
        buf = getattr(self, "_self_sends", None)
        if buf is None:
            buf = self._self_sends = deque(maxlen=400)
        return buf

    def _note_self_send(self, account: str, buyer_id: str, content: str) -> None:
        """记下「本桥接（中心指令 / AI）刚发出去」的一条消息，供本地工作台区分智能体/人工。"""
        if not _sent_text_key(content):
            return
        self._self_send_buffer().append({
            "account": str(account or ""),
            "buyer_id": str(buyer_id or ""),
            "text": str(content or ""),
            "ts": time.time(),
        })

    def _classify_sent_by(self, msg: dict) -> str:
        """mall_cs 消息是「中心指令/AI 发出的」(agent) 还是「人在客户端发的」(human)；不确定则空。"""
        if str(msg.get("role") or "") != "mall_cs":
            return ""
        content = str(msg.get("content") or "")
        if not content:
            return ""
        account = str(msg.get("account") or "")
        buyer_id = str(msg.get("buyer_id") or "")
        try:
            window = float(self.cfg.get("self_send_match_seconds") or 180)
        except (TypeError, ValueError):
            window = 180.0
        now = time.time()
        for row in list(getattr(self, "_self_sends", None) or []):
            if window > 0 and (now - float(row.get("ts") or 0)) > window:
                continue
            if row.get("account") and account and not _same_seat(row["account"], account):
                continue
            if row.get("buyer_id") and buyer_id and row["buyer_id"] != buyer_id:
                continue
            if _same_sent_text(row.get("text") or "", content):
                return "agent"
        # 历史补拉的帧不参与「人工」判定：那时到底是谁发的已无从考证，宁可留空让前端退回旧启发式
        if msg.get("is_history"):
            return ""
        return "human"

    def _ensure_result_reporter(self) -> None:
        """后台结果回报线程：把“回报中心”从命令关键路径上拿掉。"""
        if getattr(self, "_result_reporter_started", False):
            return
        self._result_reporter_started = True
        self._result_report_queue: "queue.Queue" = queue.Queue()
        self._result_report_pending: set = set()
        self._result_report_lock = threading.Lock()

        def run() -> None:
            while not self._stop.is_set():
                try:
                    command_id, result = self._result_report_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    self._send_command_result(command_id, result)
                except Exception as exc:  # 后台线程绝不死掉；journal 会重试
                    log.warning("report result worker error: %s", exc)
                finally:
                    with self._result_report_lock:
                        self._result_report_pending.discard(command_id)

        threading.Thread(target=run, name="bridge-result-reporter", daemon=True).start()

    def _report_command_result(self, command_id: str, result: dict) -> bool:
        """异步回报：命令线程不再同步等中心 ACK（实测一次约 9s）。

        结果已先落 command_journal，后台发送失败/未提交时由 _retry_command_results 继续重试。
        """
        if not command_id:
            return False
        self._ensure_result_reporter()
        with self._result_report_lock:
            if command_id in self._result_report_pending:
                return True
            self._result_report_pending.add(command_id)
        self._result_report_queue.put((command_id, dict(result)))
        return True

    def _send_command_result(self, command_id: str, result: dict) -> bool:
        try:
            response = self.client.report_command_result(command_id, result)
        except BridgeClientError as exc:
            self._last_error = f"report_command_result: {exc}"
            log.warning("report result failed: %s", exc)
            acknowledgement = exc.payload.get("command_ack") if isinstance(exc.payload, dict) else None
            stale_lease = (
                exc.status == 409
                and isinstance(acknowledgement, dict)
                and acknowledgement.get("retryable") is False
            )
            if exc.status in {404, 410} or stale_lease:
                removed = self.command_journal.remove(command_id)
                self._commands_done.add(command_id)
                self._last_error = "" if removed else self.command_journal.last_error
            return False
        acknowledgement = (
            response.get("command_ack")
            if isinstance(response, dict) and isinstance(response.get("command_ack"), dict)
            else {}
        )
        committed = bool(acknowledgement.get("committed"))
        if not acknowledgement and isinstance(response, dict):
            committed = bool(response.get("ok"))
        if not committed:
            self._last_error = f"report_command_result: command {command_id} awaiting ACK"
            return False
        removed = self.command_journal.remove(command_id)
        self._commands_done.add(command_id)
        if len(self._commands_done) > 5000:
            self._commands_done = set(list(self._commands_done)[-2000:])
        if not removed:
            self._last_error = self.command_journal.last_error
        elif self._last_error.startswith((
            "report_command_result:",
            "load_command_journal:",
            "write_command_journal:",
        )):
            self._last_error = ""
        return True

    def _retry_command_results(self) -> None:
        for command_id, result in self.command_journal.pending_results():
            if not self._report_command_result(command_id, result):
                break

    def _history_pull_loop(self) -> None:
        """周期性对“最近出现过的会话”走 CDP 回拉历史。

        拼多多网页工作台只推送“当前激活会话”的消息；未激活会话的消息（探域也不会写
        日志）靠这个循环主动拉回来，作为 tanyu_logs 数据源的兜底。入站消息按
        platform_message_id 生成 event_id，与主通道自然去重。
        """
        try:
            interval = float(self.cfg.get("history_pull_seconds") or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0 or self.pddbridge_source is None:
            return

        def _int(key, default):
            try:
                return int(self.cfg.get(key) or default)
            except (TypeError, ValueError):
                return default

        size = max(1, min(_int("history_pull_size", 20), 200))
        max_buyers = max(1, min(_int("history_pull_max_buyers", 40), 500))
        try:
            gap = max(0.0, float(self.cfg.get("history_pull_gap_seconds") or 0.3))
        except (TypeError, ValueError):
            gap = 0.3
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            recent = getattr(self, "_recent_buyers", None) or {}
            targets = sorted(recent.items(), key=lambda kv: kv[1], reverse=True)[:max_buyers]
            pulled = 0
            for (account, buyer), _ts in targets:
                if self._stop.is_set():
                    break
                try:
                    self.pddbridge_source.pull_history(buyer, account, size=size, timeout=5.0)
                    pulled += 1
                except Exception as exc:  # 单条失败不影响其他会话
                    log.debug("history pull failed (%s/%s): %s", account, buyer, exc)
                self._stop.wait(gap)
            if pulled and not self._stop.is_set():
                log.info("history pull: %d conversation(s) backfilled", pulled)

    def _command_loop(self) -> None:
        # 中心若不在长轮询里提前返回，20s 的等待会让 AI 回复排名延迟几十秒
        # （实测“创建→执行”为 4.6s / 30.8s / 48.7s，也是自动命令被判过期的深层原因）。
        # 用配置的 command_poll_seconds（默认 1.5s）并收敛到 0.5–5s，兼顾时延与请求量。
        try:
            poll = float(self.cfg.get("command_poll_seconds") or 1.5)
        except (TypeError, ValueError):
            poll = 1.5
        wait = max(0.5, min(poll, 5.0))
        # 并发轮询条数。中心那个接口每次固定要 ~9.15 秒（实测，与 wait_seconds 无关），
        # 单线程 = 每 10.7 秒才能取一次指令；开 N 条把间隔压到约 (9.15+wait)/N。
        # 默认 1 = 保持原行为；实测有 9 秒固定开销时建议 3。
        try:
            workers = int(self.cfg.get("command_poll_workers") or 3)
        except (TypeError, ValueError):
            workers = 1
        workers = max(1, min(workers, 8))
        if workers > 1:
            # 补报结果只由主线程跑，避免同一份结果被 N 条线重复上报。
            self._skip_result_retry = True
            for _index in range(workers - 1):
                threading.Thread(target=self._command_poll_worker, args=(wait,),
                                 name="cmd-poll-%d" % _index, daemon=True).start()
            log.info("命令长轮询并发 %d 条（取指令间隔约 %.1fs -> 约 %.1fs）",
                     workers, 9.15 + wait, (9.15 + wait) / workers)
        # 出站并发池：只有 command_sender_shop_ids 非空（灰度命中）才真的起线程。
        self._start_sender_pool()
        log.info("命令长轮询已启动（wait_seconds=%.1f）。收到指令会打 "
                 "\"command <id> type=...\" 日志；一条都没有 = 指令没下来", wait)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._handle_commands(wait_seconds=wait)
            except Exception as exc:
                # 单条异常指令绝不能杀死长轮询线程：线程静默退出后心跳和界面都还
                # 正常，但命令再也不会被执行，且没有任何可见报错。
                self._last_error = f"handle_commands: {exc}"
                log.exception("命令处理异常（线程继续）")
            # 单次轮询耗时 = 指令能迟到多久的上限。中心挂住但没超时的话，
            # 这里一直不响，日志上就只能看到"指令怎么迟到了"，看不出被堵在哪。
            elapsed = time.monotonic() - started
            if elapsed > wait + 2.0:
                self._log_throttled(
                    "slow_poll",
                    "命令轮询单次耗时 %.1fs（预期约 %.1fs）—— 中心那端可能挂住了，"
                    "这期间新指令收不到，会表现为「客户收到回复迟了几秒」",
                    elapsed, wait, interval=30.0)
            self._stop.wait(0.2)

    def _heartbeat(self) -> None:
        status = self._status_payload()
        self._last_status = dict(status)
        with self._pending_lock:
            metrics = queue_metrics(self._pending)
        status["event_queue"] = metrics
        log.info("event queue pending=%d oldest_wait=%.3fs capture_delay_max=%.3fs history=%d",
                 metrics["pending"], metrics["oldest_wait_seconds"],
                 metrics["max_capture_delay_seconds"], metrics["history_pending"])
        try:
            response = self.client.heartbeat(status)
            profile = response.get("parser_profile") if isinstance(response, dict) else None
            if profile is not None and self.platform.name == "pdd":
                from .parser import configure_parser_profile

                configure_parser_profile(
                    profile,
                    self.cfg.get("parser_profile_cache_path") or "",
                )
            self._last_heartbeat_ok = True
            keep_error = self._last_error.startswith((
                "upload",
                "report_command_result:",
                "load_command_journal:",
                "write_command_journal:",
            ))
            self._last_error = self._last_error if keep_error else ""
        except BridgeClientError as exc:
            self._last_heartbeat_ok = False
            self._last_error = f"heartbeat: {exc}"
            log.warning("暂时连不上中心，将自动重试：%s", exc)

    def run_forever(self) -> None:
        log.info(
            "%s 桥接助手 %s 启动  工位=%s  服务器=%s",
            self.platform.label,
            __version__,
            self.cfg.get("agent_name") or self.cfg.get("agent_id"),
            self.cfg.get("server_url"),
        )
        if not self.cfg.get("agent_token"):
            raise SystemExit(
                "agent_token 未配置。请编辑配置文件或设置环境变量 BRIDGE_AGENT_TOKEN"
            )
        # Taobao/openbot: local WS inject bridge (no 探域 DevTools required)
        if self.platform.name == "taobao":
            try:
                from .openbot_ws import start_openbot_bridge, status_snapshot
                from .qn_inject import inject, inject_status

                start_openbot_bridge()
                inj = inject_status()
                log.info("openbot inject status: %s", inj)
                if not inj.get("all_injected"):
                    res = inject(prefer_local=True, force=False)
                    log.info("openbot inject attempt: %s", res)
                    if not res.get("ok"):
                        log.warning(
                            "千牛桥接未全部注入成功：%s（请关闭千牛后，以管理员身份重开 Agent 再试）",
                            res.get("error") or res,
                        )
                else:
                    log.info(
                        "千牛 openbot 桥已全部注入 count=%s mode=%s",
                        inj.get("injected_count"),
                        inj.get("mode"),
                    )
                log.info("openbot ws status: %s", status_snapshot())
            except Exception as exc:
                log.warning("openbot bridge bootstrap failed: %s", exc)
        try:
            reg = self.client.register()
            self._apply_registered_agent_id(reg)
            log.info("已连接中心，注册成功（%s）", self.platform.label)
            log.debug("register detail: %s", reg)
        except BridgeClientError as exc:
            log.warning("暂时连不上中心，将自动重试：%s", exc)
            self._last_error = f"register: {exc}"

        # 注册之后再起 WS：服务端按 token 解析身份，握手带的 X-Agent-Id 必须与之一致，
        # 否则会被直接拒绝。连不上不影响 HTTP 上行。
        if self.center_ws is not None:
            if self.center_ws.start():
                log.info("中心 WS 上行已启用: %s", self.center_ws.ws_url)
            else:
                log.warning("中心 WS 上行启动失败，继续用 HTTP 上报: %s",
                            self.center_ws.last_error)

        self._start_feeds()
        if self.platform.name == "pdd" and self.pddbridge_source is not None:
            try:
                pull_seconds = float(self.cfg.get("history_pull_seconds") or 0)
            except (TypeError, ValueError):
                pull_seconds = 0
            if pull_seconds > 0:
                # 主源不是 CDP 时，CDP 只用来补历史，必须切到"只补历史"模式：
                # 否则这个源同时把实时也收了，同一条消息被两条通道各收一遍
                # （实测 10/32 的 event_id 被上传两遍）。
                # 这里和 _start_feeds 的二选一保持一致：数据源互斥，CDP 只做补拉。
                self.pddbridge_source.history_only = self._effective_source != "cdp"
                try:
                    self.pddbridge_source.start()
                except Exception as exc:
                    log.warning("CDP 历史补拉启动失败(沿用探域日志): %s", exc)
                else:
                    log.info("已开启 CDP 历史补拉：每 %.0fs 对最近 %s 个会话回拉历史（只补历史=%s）",
                             pull_seconds, self.cfg.get("history_pull_max_buyers") or 40,
                             self.pddbridge_source.history_only)
                    threading.Thread(target=self._history_pull_loop,
                                     name="bridge-history-pull", daemon=True).start()
        command_thread = threading.Thread(
            target=self._command_loop,
            name="bridge-command-long-poll",
            daemon=True,
        )
        command_thread.start()
        hb_every = max(2.0, float(self.cfg.get("heartbeat_seconds") or 5.0))
        last_hb = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_hb >= hb_every:
                    self._heartbeat()
                    self._flush_events()
                    self._drain_center_ws_results()
                    last_hb = now
                self._stop.wait(0.2)
        finally:
            self._stop.set()
            self.watcher.stop()
            command_thread.join(timeout=1.0)
            log.info("桥接已停止")

    def stop(self) -> None:
        self._stop.set()
        try:
            self.watcher.stop()
        except Exception:
            pass
        channel = getattr(self, "center_ws", None)
        if channel is not None:
            try:
                # 先把还没 ack 的事件排空再断连接，否则它们要等下一次启动才重投。
                self._flush_events()
            except Exception:
                pass
            try:
                channel.stop()
            except Exception:
                pass
        if self.pddbridge_source is not None:
            try:
                self.pddbridge_source.stop()
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="探域桥接客户端")
    parser.add_argument("--config", default="", help="bridge_config.json 路径")
    parser.add_argument("--platform", default="", help="pdd | taobao")
    parser.add_argument("--init-config", action="store_true", help="生成示例配置后退出")
    parser.add_argument("--status", action="store_true", help="打印本机通道状态后退出")
    parser.add_argument("--dry-run", action="store_true", help="发送命令不调真实通道")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    plat = args.platform or ""
    if args.init_config:
        path = write_example_config(
            Path(args.config) if args.config else None,
            platform=plat or "pdd",
        )
        print(f"wrote example config: {path}")
        return 0

    cfg = load_config(Path(args.config) if args.config else None, platform=plat)
    if args.dry_run:
        cfg["dry_run"] = True

    platform = get_platform(cfg.get("platform"))
    if args.status:
        st = platform.channel_status(cfg)
        st["tanyu_log_dir"] = cfg.get("tanyu_log_dir")
        st["log_dir_exists"] = Path(str(cfg.get("tanyu_log_dir") or "")).is_dir()
        st["server_url"] = cfg.get("server_url")
        st["agent_id"] = cfg.get("agent_id")
        st["agent_token_set"] = bool(cfg.get("agent_token"))
        st["platform"] = platform.name
        st["platform_label"] = platform.label
        st["dry_run"] = bool(cfg.get("dry_run"))
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    agent = BridgeAgent(cfg)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
        print("stopped")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
