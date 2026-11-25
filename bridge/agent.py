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
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import __build_hash__, __product__, __version__
from .client import BridgeClient, BridgeClientError
from .command_journal import CommandJournal
from .event_queue import append_event
from .config import load_config, save_config, write_example_config
from .platforms import get_platform
from .watcher import LogWatcher
from .message_timing import (TIMING_FIELDS, FUTURE_TOLERANCE_SECONDS,
                             TAKEOVER_PARENT_MAX_AGE_SECONDS, epoch_seconds,
                             stamp_message, history_ready, command_expired, queue_metrics,
                             hold_remaining)

log = logging.getLogger("pdd.bridge")

# 出站内容归一化：去掉 emoji/标点/空白，只留字母数字与汉字，用于判定“同一次发送”
_OUTGOING_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


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
            websocket_enabled=bool(self.cfg.get("websocket_enabled", False)),
            websocket_url=str(self.cfg.get("websocket_url") or ""),
            websocket_path=str(self.cfg.get("websocket_path") or "/api/bridge/v1/ws"),
            websocket_max_inflight=int(self.cfg.get("websocket_max_inflight") or 64),
        )
        self._stop = threading.Event()
        # Wake uploads as soon as a live event is queued; heartbeat remains the fallback.
        self._events_wakeup = threading.Event()
        try:
            upload_workers = int(self.cfg.get("upload_concurrency") or 8)
        except (TypeError, ValueError):
            upload_workers = 8
        self._upload_workers = max(1, min(upload_workers, 16))
        self._upload_executor = ThreadPoolExecutor(
            max_workers=self._upload_workers,
            thread_name_prefix="bridge-event-upload",
        )
        self._upload_dispatch_lock = threading.Lock()
        self._upload_active = 0
        self._pending: Deque[dict] = deque()
        self._pending_lock = threading.Lock()
        self._accounts_seen: Dict[str, float] = {}
        self._scope_filtered_events = 0
        self._scope_blocked_commands = 0
        self._last_heartbeat_ok = False
        self._last_error = ""
        self._last_status: dict = {}
        self._commands_done: set[str] = set()
        # 投递台账: 每条消息的 采集/本地工作台/中心 三个环节逐条留痕
        from .ledger import DeliveryLedger
        queue_file = str(self.cfg.get("local_queue_path") or "bridge_queue.jsonl")
        self._ledger = DeliveryLedger(
            self.cfg.get("delivery_ledger_path") or queue_file.replace(".jsonl", "_ledger.jsonl"),
            enabled=bool(self.cfg.get("delivery_ledger_enabled", True)),
        )
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
        self._data_source = str(self.cfg.get("data_source") or "tanyu_logs")
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
            return mapped
        if account.startswith("cs_"):
            mall = account[3:].split(":", 1)[0].split("_", 1)[0]
            return f"mall_{mall}" if mall.isdigit() else ""
        if account.startswith(("mall_", "tb_")):
            return account
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
        from .pdd_system_messages import is_pdd_system_event
        if (msg.get("platform") or self.platform.name) == "pdd" and is_pdd_system_event(msg):
            msg = {**msg, "is_diagnostic": True, "skipped_reason": "pdd_operator_notice"}
            log.info("PDD operator notice excluded from buyer stream msg_id=%s account=%s",
                     msg.get("msg_id"), msg.get("account"))
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
        account = str(msg.get("account") or "").strip()
        if account:
            self._accounts_seen[account] = time.time()
        if account and msg.get("buyer_id") and not msg.get("is_diagnostic"):
            # 记「活跃会话」，供 CDP 周期补拉历史。
            # 0.7.11：以前只认实时帧（not is_history），结果实时通道一死，候选表就是空的，
            # 连回扫都不触发（现场：pending=0 全程、本地队列 0 字节）。
            # 现在任何入站消息都记，并持久化，重启不丢。
            self._note_history_target(str(account), str(msg.get("buyer_id")), bool(msg.get("is_history")))
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
                    # 待发队列里已有同一条：如果那条是"历史"而这条是"实时"，
                    # 直接把历史副本替换掉（Phase-3 升级），否则才是真重复。
                    if self._replace_pending_history(event):
                        self._ledger_note(event["event_id"], "history_upgraded", event=event,
                                          detail="历史副本被实时帧替换")
                        return
                    log.info("event duplicate skipped id=%s（已在本机待发队列里）", event["event_id"])
                    self._ledger_note(event["event_id"], "center_duplicate_skipped", event=event,
                                        detail="已在本机待发队列中")
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
            wakeup = getattr(self, "_events_wakeup", None)
            if wakeup is not None:
                wakeup.set()
            # Submit from the capture thread as well as waking the control loop.
            # The control loop intentionally polls at a low rate; relying on it
            # alone can merge a burst of events into one worker and make later
            # events wait behind a slow /events request.
            self._schedule_event_flush()
        if self._local_ingest_queue is not None:
            try:
                self._local_ingest_queue.put_nowait(event)
            except Exception:
                pass

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
        # Shop identity for brain-server merge (1 shop many CS accounts).
        shops: List[dict] = []
        seen_shop_acc: set = set()
        for acc in accounts:
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
            "dll_ready": bool(ch.get("dll_ready")),
            "send_ready": bool(ch.get("send_ready", ch.get("dll_ready"))),
            "send_channel": str(ch.get("send_channel") or ""),
            "receive_ready": bool(ch.get("receive_ready", True)),
            "dll_port": ch.get("dll_port"),
            "cdp_port": cdp_port,
            "workbench_pid": ch.get("workbench_pid"),
            "port_discovery": ch.get("port_discovery"),
            "channel_hint": ch.get("hint") or "",
            "hint": ch.get("hint") or "",
            "last_error": self._last_error or src_last_error or self.watcher.last_error,
            "websocket": self.client.transport_status(),
            "dry_run": bool(self.cfg.get("dry_run")),
            # 采集健康度：“注入成功”不等于“有数据”，这两组字段就是为此而加。
            "capture": self._capture_status(cdp_stats),
            "history_pull": getattr(self, "_history_pull_stats", {}),
            "history_targets": len(getattr(self, "_recent_buyers", None) or {}),
        }

    def _capture_status(self, cdp_stats: dict | None = None) -> dict:
        """真实采集状态：最近有没有帧、卡在哪里。"""
        if self._effective_source == "cdp" and cdp_stats is not None:
            if not cdp_stats.get("injected"):
                return {"ok": False, "reason": "not_injected",
                        "detail": "工作台页面未注入（未发现 CDP 端口或会话）"}
            frames = int(cdp_stats.get("frames_last_5m") or 0)
            if frames > 0:
                return {"ok": True, "reason": "frames", "frames_last_5m": frames}
            sockets = sorted({str(s.get("socket_state") or "?") for s in (cdp_stats.get("sessions") or [])})
            return {"ok": False, "reason": "no_frames",
                    "frames_last_5m": 0,
                    "last_frame_at": cdp_stats.get("last_frame_at") or 0,
                    "socket_states": sockets,
                    "detail": "已注入但最近 5 分钟零帧：页面无 JS socket 时需原生接收或探域日志"}
        # 日志通道：看 watcher 真的读到新行没有（探域没跑时这里是 0）
        try:
            diag = self.watcher.diagnostics()
        except Exception:
            diag = {}
        if not diag.get("files"):
            return {"ok": False, "reason": "no_log_files", "stats": diag,
                    "detail": "没找到可监听的日志文件（探域未安装/未启动？）"}
        last_line = float(diag.get("last_line_at") or 0.0)
        try:
            idle_limit = max(60.0, float(self.cfg.get("capture_warn_seconds") or 600.0))
        except (TypeError, ValueError):
            idle_limit = 600.0
        idle = (time.time() - last_line) if last_line else None
        if last_line and idle is not None and idle <= idle_limit:
            return {"ok": True, "reason": "log_source", "idle_seconds": round(idle, 1),
                    "stats": diag}
        return {"ok": False, "reason": "log_idle", "idle_seconds": idle,
                "stats": diag,
                "detail": "日志通道已 %.0f 分钟没有新行（探域可能没在跑）"
                          % ((idle or idle_limit) / 60.0)}

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
        """CDP 源不可用时的处理（PddbridgeSource 降级回调, 线程安全）。

        0.7.13 起 PDD 不再自动降级到探域日志：探域要么已卸载、要么没运行，
        降级过去等于把唯一的数据源也关掉（现场 KD5MD2C 就是这样彻底没数据）。
        现在保持 CDP 持续重试，并在心跳里把原因写清楚。
        """
        # 只有显式配置了探域日志、且目录真的存在时才允许降级（存量客户过渡期）
        log_dir = str(self.cfg.get("tanyu_log_dir") or "")
        try:
            usable = bool(log_dir) and Path(log_dir).is_dir()
        except OSError:
            usable = False
        if str(self._data_source or "").strip().lower() != "tanyu_logs" or not usable:
            self._last_error = ("CDP 通道未就绪（工作台未启动/未开调试端口/未进接待页），"
                                "保持重试，不降级到探域日志")
            log.warning("CDP 未就绪，保持重试（data_source=%s, 日志目录可用=%s）",
                        self._data_source, usable)
            return
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
        except OSError as exc:
            log.warning("record refused events failed: %s", exc)

    def _upload_batch_size(self) -> int:
        """每批上送中心的条数。突发进线时批越小、批数越多，而每批都要等一次本地优先
        门控（~1s），尾部就迟到得越久；可用 upload_batch_size 调大（上限 2000）。"""
        try:
            value = int(self.cfg.get("upload_batch_size") or 0)
        except (TypeError, ValueError):
            value = 0
        return max(1, min(value or 500, 2000))

    def _history_window(self) -> float:
        """历史事件要等多久才上送（秒）。

        ``history_hold_policy``：
          * ``legacy``（默认）→ 90 秒，兼容不认 is_history 的老中心；
          * ``ack_capable`` → 0 秒，中心已按 is_history 自行决定回不回，不必压。
        ``history_live_window_seconds`` 可直接指定秒数，优先级最高。
        """
        policy = str(self.cfg.get("history_hold_policy") or "legacy").strip().lower()
        explicit = set(self.cfg.get("_explicit_keys") or ())
        # load_config 总会把 history_live_window_seconds 补成 90，
        # 老实现直接读它 → ack_capable 永远无效（现场"设了也没用"就是这个）。
        # 只有用户真的在配置文件里写过这个键，才让它压过 policy。
        if "history_live_window_seconds" not in explicit:
            if policy in {"ack_capable", "off", "none", "disabled"}:
                return 0.0
            if policy in {"legacy", "", "default"}:
                return max(0.0, float(self.cfg.get("history_live_window_seconds") or 90.0))
        try:
            value = float(self.cfg.get("history_live_window_seconds"))
        except (TypeError, ValueError):
            value = 90.0
        return max(0.0, value)

    def _queue_hold(self, pending, now: float) -> dict:
        """算清楚"队列里这些事件为什么还没发"，给心跳日志用。

        旧实现只打 oldest_wait，客户和我们自己都分不清是等历史门槛、在途、
        重试，还是等本地优先队列——每次都靠猜。
        """
        window = self._history_window()
        inflight = set(getattr(self, "_inflight_ids", None) or ())
        retrying = time.monotonic() < getattr(self, "_upload_retry_at", 0)
        counts: dict[str, int] = {}
        remaining = float("inf")
        for event in pending:
            event_id = str(event.get("event_id") or "")
            if event_id in inflight:
                counts["inflight"] = counts.get("inflight", 0) + 1
                continue
            if retrying:
                counts["client_retry"] = counts.get("client_retry", 0) + 1
                continue
            wait = hold_remaining(event, now, window)
            if wait > 0:
                counts["hold_window"] = counts.get("hold_window", 0) + 1
                remaining = min(remaining, wait)
            else:
                counts["ready"] = counts.get("ready", 0) + 1
        if not pending:
            reason = "idle"
        else:
            # 优先报真正卡住的那一类；inflight/ready 只是瞬时状态
            for key in ("hold_window", "client_retry", "inflight", "ready"):
                if counts.get(key):
                    reason = key
                    break
            else:
                reason = "unknown"
        oldest = max(0.0, now - min(
            [float(e.get("captured_at") or 0) for e in pending] or [now]))
        return {
            "held": counts.get("hold_window", 0) + counts.get("client_retry", 0),
            "reason": reason,
            "hold_reasons": counts,
            "window_seconds": round(window, 1),
            "min_remaining_seconds": round(remaining, 1) if remaining != float("inf") else None,
            "oldest_wait_seconds": round(oldest, 3),
        }

    def _flush_events(self, *, blocked_ids: Optional[set[str]] = None) -> None:
        now = time.time()
        history_now = self.client.server_now() - 1.0 if hasattr(self.client, "server_now") else now
        if isinstance(self.client, BridgeClient) and self.client._server_clock is None:
            history_now = 0.0
        if time.monotonic() < getattr(self, "_upload_retry_at", 0):
            return
        window = self._history_window()
        with self._pending_lock:
            inflight = getattr(self, "_inflight_ids", None)
            if inflight is None:
                inflight = self._inflight_ids = set()
            for event in self._pending:
                event.update(stamp_message(event, now=now))
            ready = [e for e in self._pending
                     if str(e.get("event_id") or "") not in inflight
                     and e.get("event_id") not in (blocked_ids or set())
                     and history_ready(e, history_now if e.get("is_history") else now, window)]
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
        log.info("event upload start worker=%s batch=%d oldest_wait=%.3fs ids=%s",
                 threading.current_thread().name, len(batch),
                 queue_metrics(batch)["oldest_wait_seconds"], [e.get("event_id") for e in batch])
        queue_changed = False
        terminal_ids = set()
        try:
            response = self.client.upload_events(batch)
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
            log.info("event upload terminal worker=%s terminal=%d remaining=%d terminal_ids=%s",
                     threading.current_thread().name,
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

    def _schedule_event_flush(self) -> None:
        """Submit upload work without blocking the agent/control loop."""
        # Lightweight test doubles may call _on_local_event without running the
        # full constructor; the normal process always initializes the executor.
        if not hasattr(self, "_upload_executor") or not hasattr(self, "_upload_dispatch_lock"):
            return
        with self._upload_dispatch_lock:
            if self._upload_active >= self._upload_workers:
                return
            self._upload_active += 1

        def worker() -> None:
            try:
                self._flush_events()
            finally:
                with self._upload_dispatch_lock:
                    self._upload_active -= 1

        try:
            self._upload_executor.submit(worker)
        except RuntimeError:
            with self._upload_dispatch_lock:
                self._upload_active = max(0, self._upload_active - 1)

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
        if not hasattr(self, "_sender_tails"):
            self._sender_tails = {}
        if not hasattr(self, "_sender_pool_guard"):
            self._sender_pool_guard = threading.Lock()
        if not hasattr(self, "_sender_overflow"):
            self._sender_overflow = 0

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
                self._run_queued_command(cmd)
            except Exception:
                # 单条命令异常不能打死发送线程，否则出站会整体停摆。
                log.exception("command sender worker failed")
            finally:
                self._sender_queue.task_done()

    _SENDER_CHAIN_WAIT_FLOOR = 30.0

    def _sender_chain_wait_seconds(self) -> float:
        """等“同一买家上一条”的上限。

        无上限时，只要有一条发送卡死（工作台无响应/回执永远不来），这个买家
        后面每条消息都会跟着卡死，池子 worker 也会被逐个占满。超时后继续执行：
        顺序退化成尽力而为，但不至于把出站整体拖停。
        """
        try:
            confirm = float(self.cfg.get("send_confirm_timeout_seconds") or 10.0)
        except (TypeError, ValueError):
            confirm = 10.0
        return max(self._SENDER_CHAIN_WAIT_FLOOR, confirm * 3)

    def _run_queued_command(self, cmd: dict) -> None:
        order = cmd.pop("_local_send_order", None)
        if order is None:
            with self._sender_conversation_lock(cmd.get("account"), cmd.get("buyer_id")):
                self._execute_command(cmd)
            return
        key, previous, done = order
        try:
            if previous is not None:
                if not previous.wait(self._sender_chain_wait_seconds()):
                    log.warning("sender chain wait timed out; ordering best-effort "
                                "account=%s buyer=%s", key[0], key[1])
            self._execute_command(cmd)
        finally:
            done.set()
            with self._sender_conv_locks_guard:
                if self._sender_tails.get(key) is done:
                    del self._sender_tails[key]

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

    def _release_claim(self, command_id: str) -> None:
        """撤销认领：journal 落盘失败时这条指令必须还能在下一轮重试。

        认领集合没有过期时间，一旦占了位就再也不会被放出来 —— 直到进程重启。
        """
        lock = getattr(self, "_command_claim_lock", None)
        if lock is None:
            return
        with lock:
            getattr(self, "_command_claimed", set()).discard(command_id)

    def _handle_commands(self, *, wait_seconds: float = 0.0) -> None:
        self._retry_command_results()
        drain_ws_commands = getattr(self.client, "drain_ws_commands", None)
        commands = drain_ws_commands() if callable(drain_ws_commands) else []
        try:
            http_commands = self.client.pull_commands(wait_seconds=wait_seconds)
            commands.extend(http_commands)
        except BridgeClientError as exc:
            self._last_error = f"pull_commands: {exc}"
            log.warning("command pull failed status=%s: %s", exc.status, exc)
            if not commands:
                return
        if commands:
            log.info("command received=%d agent_id=%s", len(commands),
                     getattr(self.client, "agent_id", self.cfg.get("agent_id", "")))
        self._ensure_sender_state()
        inline: List[dict] = []
        for cmd in commands:
            account = str((cmd or {}).get("account") or "").strip()
            if self._command_sender_enabled(account):
                key = (account, str(cmd.get("buyer_id") or "").strip())
                done = threading.Event()
                with self._sender_conv_locks_guard:
                    previous = self._sender_tails.get(key)
                    self._sender_tails[key] = done
                cmd = dict(cmd, _local_send_order=(key, previous, done))
                if self._enqueue_command(cmd):
                    continue
                # 池子满：绝不能在主轮询线程里内联执行 —— 那条路径要等前一条
                # 发送完成（可能 10s+），会把取指令和心跳一起卡住。
                log.warning("command sender pool saturated; handing off: %s",
                            str((cmd or {}).get("id")
                                or (cmd or {}).get("command_id") or ""))
                self._handoff_command(cmd)
                continue
            inline.append(cmd)
        for cmd in inline:
            self._execute_command(cmd)

    def _enqueue_command(self, cmd: dict) -> bool:
        try:
            self._sender_queue.put_nowait(cmd)
            return True
        except queue.Full:
            return False

    def _handoff_command(self, cmd: dict) -> None:
        """池子满时的兜底：交给短命线程，保持链式顺序且不阻塞轮询线程。"""
        self._ensure_sender_state()
        with self._sender_pool_guard:
            if self._sender_overflow >= 16:
                log.error("command sender overflow exhausted; dropping command %s",
                          str((cmd or {}).get("id") or (cmd or {}).get("command_id") or ""))
                return
            self._sender_overflow += 1
        thread = threading.Thread(
            target=self._sender_overflow_task, args=(cmd,),
            name="bridge-command-sender-overflow", daemon=True,
        )
        thread.start()

    def _sender_overflow_task(self, cmd: dict) -> None:
        try:
            self._run_queued_command(cmd)
        except Exception:
            log.exception("command sender overflow worker failed")
        finally:
            with self._sender_pool_guard:
                self._sender_overflow = max(0, self._sender_overflow - 1)

    def _execute_command(self, cmd: dict) -> None:
        command_id = str(cmd.get("id") or cmd.get("command_id") or "").strip()
        if not command_id:
            log.warning("command pull returned item without id")
            return
        existing = self.command_journal.get(command_id)
        if existing is not None:
            if existing.get("state") == "result_pending":
                log.info("command %s duplicate; result_pending, retrying ACK", command_id)
                self._report_command_result(command_id, dict(existing.get("result") or {}))
            else:
                log.info("command %s duplicate; state=%s, skip execution", command_id,
                         existing.get("state"))
            return
        if not self._claim_command(command_id):
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
            # 认领必须放回去，否则这条指令在本进程里永远拿不回来（journal
            # 只是暂时不可写时会丢一条回复，且只能靠重启恢复）。
            self._release_claim(command_id)
            return
        if cmd_type not in {"send_text", "send_image", "open_chat", "pull_history", "session_state"}:
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
        elif cmd_type == "send_image":
            gate = self._order_gate(cmd, account, buyer_id)
            if gate is not None:
                result = gate
            else:
                result = self._send_images_command(cmd, account, buyer_id)
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
                result = src.pull_history(
                    buyer_id, account, size=want_n,
                    begin_msg_id=cmd.get("begin_msg_id") or 0,
                    start_index=int(cmd.get("start_index") or 0),
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
            # 催付/跟进类命令：发之前先查「这个人买了没有」。
            # 客户口径：没买（无单/未付款/已关闭）都可催，只有已支付才拦。
            gate = self._order_gate(cmd, account, buyer_id)
            if gate is not None:
                self._finalize_command(command_id, cmd, gate)
                return
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
            # 出站是单线程串行的（一次只发一条），这条耗时直接决定“AI 回复吞吐”上限：
            # 容量 ≈ 1 / 平均耗时。实测日志里一半以上的指令→回显超过 10s，
            # 需要区分是“探域 DLL 慢”还是“发送确认没匹配上干等到超时”。
            # 话术库常常是「文字 + 图片」：文字发成功后继续把图片发出去，
            # 结果合并到同一条命令回执里（部分失败也要如实回报）。
            extra = self._send_followup_images(cmd, account, buyer_id, result)
            if extra:
                result = dict(result or {})
                result["images"] = extra.get("images")
                if extra.get("image_errors"):
                    result["image_errors"] = extra["image_errors"]
                if not result.get("ok") and extra.get("ok"):
                    result["ok"] = True
                    result["status"] = "sent"
                    result["real_send"] = True
            duration = time.monotonic() - started
            log.info("command %s send done in %.2fs status=%s real_send=%s via=%s",
                     command_id, duration,
                     str((result or {}).get("status") or "") if isinstance(result, dict) else "",
                     (result or {}).get("real_send") if isinstance(result, dict) else None,
                     str((result or {}).get("via") or "") if isinstance(result, dict) else "")
            # 把这次回复写进本地会话，浮窗倒计时才会停（插件日志没有卖家消息）
            self._publish_self_reply(account, buyer_id, content, result,
                                     extra.get("images") if extra else None)
        if not isinstance(result, dict):
            result = {"ok": bool(result), "raw_result": str(result or "")}
        self._finalize_command(command_id, cmd, result)

    def _publish_self_reply(self, account: str, buyer_id: str, content: str,
                           result: dict, images: list | None = None) -> None:
        """把自己发出去的回复也写进本地会话。

        插件日志（tanyu_logs）只有 buyer_msg，不包含卖家发出的消息；
        发送成功只回报中心，本地会话里永远只有买家的话 → 浮窗倒计时一直不停。
        所以发送后主动补一条 role=mall_cs 的本地事件（只推本地，不上传中心）。
        """
        if not self.cfg.get("dual_write_local_workbench", True):
            return
        # 正式运行路径（run_pdd_client.install_local_first）里 _local_ingest_queue
        # 恒为 None：那时本地投递走 LocalDeliveryQueue（_local_delivery）。只认
        # 前者会让这条修复变成死代码（0.8.1 实测整场 0 条 published 日志）。
        delivery = getattr(self, "_local_delivery", None)
        q = getattr(self, "_local_ingest_queue", None)
        if delivery is None and q is None:
            return
        result = result if isinstance(result, dict) else {}
        status = str(result.get("status") or "").strip().lower()
        ok = bool(result.get("ok"))
        if result.get("via") == "dry_run" or (self.cfg.get("dry_run") and not result.get("real_send")):
            return
        if not ok and status != "indeterminate":
            return   # 明确失败的不写，避免把“没发出去”当成已回复
        now = time.time()
        text = str(content or "").strip()
        if not text and not images:
            return
        if not text:
            text = "[图片]"
        delivered = ok and result.get("real_send") is True and status in {"confirmed", "sent", "accepted"}
        delivery_status = "confirmed" if delivered else "indeterminate"
        event = {
            "event_id": f"selfsend-{buyer_id}-{int(now * 1000)}",
            "platform": self.platform.name,
            "account": str(account or ""),
            "buyer_id": str(buyer_id or ""),
            "role": "mall_cs",
            "content": text,
            "ts": now,
            "msg_id": str(result.get("msg_id") or ""),
            "delivery_status": delivery_status,
            "source": "bridge_self_send",
            "is_history": False,
            "captured_at_ms": int(now * 1000),
        }
        try:
            if delivery is not None:
                delivery.enqueue(event)
            else:
                q.put_nowait(event)
            log.info("self reply published locally buyer=%s delivery=%s",
                     buyer_id, delivery_status)
        except Exception as exc:
            log.debug("self reply publish skipped: %s", exc)

    def _finalize_command(self, command_id: str, cmd: dict, result: dict) -> None:
        """存结果 + 回报中心（闸门拦截和真正发送走同一条收尾路径）。"""
        result = dict(result)
        result = _command_result_for_ack(result)
        result.setdefault("command_lease_token", str(cmd.get("lease_token") or ""))
        if not self.command_journal.store_result(command_id, result):
            self._last_error = self.command_journal.last_error
            log.error("command %s result journal write failed: %s", command_id,
                      self.command_journal.last_error)
        self._report_command_result(command_id, result)

    def _self_send_buffer(self) -> Deque[dict]:
        buf = getattr(self, "_self_sends", None)
        if buf is None:
            buf = self._self_sends = deque(maxlen=400)
        return buf

    @staticmethod
    def _command_images(cmd: dict) -> list:
        """从命令里取出图片列表（兼容几种常见字段）。"""
        meta = cmd.get("meta") if isinstance(cmd.get("meta"), dict) else {}
        values = []
        for holder in (cmd, meta):
            for key in ("images", "image_list", "image_urls", "pics"):
                value = holder.get(key)
                if isinstance(value, (list, tuple)):
                    values.extend(str(v) for v in value if v)
                elif isinstance(value, str) and value.strip():
                    values.append(value.strip())
            for key in ("image", "image_url", "image_path", "pic", "picture", "media_url"):
                value = holder.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
        # send_image 的正文本身就是图片地址
        if str(cmd.get("type") or "").strip() == "send_image":
            content = str(cmd.get("content") or "").strip()
            if content:
                values.insert(0, content)
        seen, out = set(), []
        for value in values:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out

    def _send_images_command(self, cmd: dict, account: str, buyer_id: str) -> dict:
        """type=send_image：把命令里的图片发出去。"""
        images = self._command_images(cmd)
        if not images:
            return {"ok": False, "status": "blocked", "real_send": False, "retryable": False,
                    "error": "no_image_in_command", "error_user": "命令里没有图片地址"}
        extra = self._send_images(images, account, buyer_id, cmd)
        results = extra.get("images") or []
        ok_count = sum(1 for item in results if item.get("ok"))
        first_error = next((item for item in results if not item.get("ok")), None)
        result = {
            "ok": ok_count > 0,
            "status": "sent" if ok_count == len(results) and ok_count else "failed",
            "real_send": ok_count > 0,
            "retryable": ok_count == 0,
            "via": "dll_direct_image",
            "images": results,
        }
        if extra.get("image_errors"):
            result["image_errors"] = extra["image_errors"]
        if first_error:
            result.setdefault("error", first_error.get("error") or "image_send_failed")
            result.setdefault("error_user", first_error.get("error_user") or "图片发送失败")
        return result

    def _send_followup_images(self, cmd: dict, account: str, buyer_id: str,
                              text_result: dict | None) -> dict | None:
        """文字命令里带的图片，文字成功后继续发。"""
        images = self._command_images(cmd)
        if not images:
            return None
        # 文字已经失败（比如被闸门拦/内容非法）就别再补图了
        if isinstance(text_result, dict) and text_result.get("ok") is False and                 text_result.get("real_send") is False:
            return None
        return self._send_images(images, account, buyer_id, cmd)

    def _send_images(self, images: list, account: str, buyer_id: str, cmd: dict) -> dict:
        from .media import localize_many

        source = self.pddbridge_source
        if source is None:
            return {"images": [], "image_errors": ["pddbridge source 未启动"], "ok": False}
        csid = cmd.get("csid") or None
        paths, errors = localize_many(images)
        results = []
        for path in paths:
            try:
                item = source.send_image(buyer_id, path, account, csid=csid)
            except Exception as exc:
                item = {"ok": False, "status": "failed", "real_send": False,
                        "error": str(exc), "error_user": "图片发送异常", "image_path": path}
            results.append(item)
            log.info("command image sent path=%s ok=%s", path, item.get("ok"))
        return {"images": results, "image_errors": errors, "ok": bool(results) and all(
            item.get("ok") for item in results)}

    def _order_lookup(self):
        """懒初始化订单查询器（与消息入线那套共用逻辑：面板定位/结果缓存/超时预算）。"""
        lookup = getattr(self, "_pdd_order_lookup", None)
        if lookup is not None:
            return lookup
        try:
            from .pdd_context import PddOrderContextLookup

            budget = max(0.4, min(3.0, float(self.cfg.get("pdd_context_timeout_seconds") or 1.8)))
            cache = max(0.0, float(self.cfg.get("pdd_context_cache_seconds") or 25.0))
            lookup = PddOrderContextLookup(timeout=budget, cache_seconds=cache)
        except Exception as exc:
            log.warning("订单闸门初始化失败（不启用）: %s", exc)
            lookup = False
        self._pdd_order_lookup = lookup
        return lookup

    def _order_gate(self, cmd: dict, account: str, buyer_id: str) -> dict | None:
        """发送前的订单闸门：按客户口径只看「付没付」。返回 None = 放行。

        ``require_order_state`` 可写在命令上（推荐），也可用配置 ``order_gate_state``
        全局打开；``not_paid`` = 「没买就放行」，只有已支付才拦。
        """
        mode = str(cmd.get("require_order_state") or self.cfg.get("order_gate_state") or "").strip().lower()
        if mode in {"", "off", "none"}:
            return None
        lookup = self._order_lookup()
        if lookup is False or lookup is None:
            return None
        try:
            window = float(cmd.get("order_window_seconds")
                           or self.cfg.get("order_gate_window_seconds") or 86400.0)
        except (TypeError, ValueError):
            window = 86400.0
        from .pdd_context import order_gate_decision, order_verdict

        raw = lookup.lookup({"account": account, "buyer_id": buyer_id})
        verdict = order_verdict(raw, window_seconds=window)
        fail_open = bool(self.cfg.get("order_gate_fail_open", False))
        allowed, reason = order_gate_decision(verdict, mode, fail_open)
        log.info("order gate buyer=%s account=%s state=%s allowed=%s reason=%s",
                 buyer_id, account, verdict.get("state"), allowed, reason)
        if allowed:
            return None
        return {
            "ok": False, "status": "blocked", "real_send": False,
            "retryable": False, "via": "order_gate",
            "error": "order_gate:%s" % reason,
            "order_state": verdict.get("state"),
            "order_id": verdict.get("order_id") or "",
            "order_age_seconds": verdict.get("age_seconds"),
            "error_user": ("买家已付款，跳过催付" if verdict.get("state") == "paid"
                           else "订单状态无法确认，已跳过（避免误催付）"),
        }

    def _replace_pending_history(self, incoming: dict) -> bool:
        """用实时事件替换待发队列里的同 event_id 历史副本。返回是否替换成功。"""
        if incoming.get("is_history"):
            return False
        event_id = str(incoming.get("event_id") or "")
        if not event_id:
            return False
        for index, pending in enumerate(self._pending):
            if str(pending.get("event_id") or "") != event_id:
                continue
            if not pending.get("is_history"):
                return False          # 两条都是实时：确实是重复
            self._pending[index] = incoming
            log.info("pending history event upgraded to live id=%s msg_id=%s",
                     event_id, str(incoming.get("msg_id") or "")[:64])
            return True
        return False

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
            log.warning("report result failed command=%s status=%s: %s",
                        command_id, exc.status, exc)
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
            log.warning("report result not committed command=%s response=%s",
                        command_id, response)
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
        log.info("report result committed command=%s", command_id)
        return True

    def _retry_command_results(self) -> None:
        for command_id, result in self.command_journal.pending_results():
            if not self._report_command_result(command_id, result):
                break

    # ---------------- 回扫目标表（落盘，冷启动不丢） ----------------
    def _history_targets_file(self) -> Path:
        path = getattr(self, "_history_targets_path", None)
        if path is None:
            base = Path(str(self.cfg.get("local_queue_path") or "bridge_queue.jsonl")).parent
            path = base / ("history_targets_%s.json" % (self.platform.name or "pdd"))
            self._history_targets_path = path
        return path

    def _ensure_history_targets(self) -> dict:
        recent = getattr(self, "_recent_buyers", None)
        if recent is None:
            recent = self._recent_buyers = {}
        # 用独立标记，而不是靠"字典是否为空"猜：字典可能早被别处建好，
        # 那样落盘的目标就永远不加载（重启后回扫直接失明）。
        if getattr(self, "_history_targets_loaded", False):
            return recent
        self._history_targets_loaded = True
        path = self._history_targets_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return recent
        ttl = self._history_target_ttl()
        now = time.time()
        for row in (data.get("targets") or []):
            try:
                account, buyer, ts = str(row[0]), str(row[1]), float(row[2])
            except (TypeError, ValueError, IndexError):
                continue
            if not account or not buyer:
                continue
            if ttl > 0 and now - ts > ttl:
                continue
            recent[(account, buyer)] = ts
        if recent:
            log.info("回扫目标表已恢复 %d 个会话", len(recent))
        return recent

    def _history_target_ttl(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("history_target_ttl_seconds") or 86400.0))
        except (TypeError, ValueError):
            return 86400.0

    def _note_history_target(self, account: str, buyer_id: str, is_history: bool = False) -> None:
        """记住该会话，供周期回扫。实时帧和回扫结果都算——

        旧实现只认实时帧，实时通道一死候选表就空了，回扫一次都不触发。
        """
        if not account or not buyer_id:
            return
        recent = self._ensure_history_targets()
        recent[(account, buyer_id)] = time.time()
        # 表不能无限长：超了就丢最久没出现的
        cap = 2000
        if len(recent) > cap:
            for key in sorted(recent, key=recent.get)[: len(recent) - cap]:
                recent.pop(key, None)
        self._save_history_targets()

    def _save_history_targets(self, force: bool = False) -> None:
        recent = getattr(self, "_recent_buyers", None) or {}
        now = time.monotonic()
        last = getattr(self, "_history_targets_saved_at", 0.0)
        if not force and now - last < 30.0:
            return
        self._history_targets_saved_at = now
        ttl = self._history_target_ttl()
        wall = time.time()
        rows = [[account, buyer, ts] for (account, buyer), ts in recent.items()
                if ttl <= 0 or wall - ts <= ttl]
        try:
            path = self._history_targets_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"version": 1, "targets": rows}, ensure_ascii=False),
                            encoding="utf-8")
        except OSError as exc:
            log.debug("回扫目标表写入失败: %s", exc)

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
            gap = max(0.0, float(self.cfg.get("history_pull_gap_seconds") or 0.05))
        except (TypeError, ValueError):
            gap = 0.05
        try:
            pull_timeout = max(1.0, float(self.cfg.get("history_pull_timeout_seconds") or 3.0))
        except (TypeError, ValueError):
            pull_timeout = 3.0
        workers = max(1, min(_int("history_pull_workers", 3), 8))
        self._ensure_history_targets()
        cursor = 0
        warned_empty = False
        # 按“起始时刻 + interval”排程，而不是“做完再等 interval”。
        deadline = time.monotonic()
        while not self._stop.is_set():
            deadline += interval
            wait_for = deadline - time.monotonic()
            if wait_for > 0 and self._stop.wait(wait_for):
                break
            if self._stop.is_set():
                break
            recent = getattr(self, "_recent_buyers", None) or {}
            ordered = sorted(recent.items(), key=lambda kv: kv[1], reverse=True)
            if not ordered:
                # 没有候选 = 回扫根本不会发生。必须大声说出来，不能静默。
                if not warned_empty:
                    warned_empty = True
                    log.warning("history pull: 没有回扫目标（实时通道无帧、也没有历史会话记录），"
                                "消息只能靠主通道，请确认探域日志/原生接收是否正常")
                continue
            warned_empty = False
            # 游标轮转：每轮从上次位置继续，保证尾部店铺也能被扫到（旧实现永远只看 top-N）
            round_targets = [ordered[(cursor + i) % len(ordered)] for i in range(min(max_buyers, len(ordered)))]
            cursor = (cursor + len(round_targets)) % len(ordered)
            started = time.monotonic()
            pulled = 0
            failed = 0
            if workers == 1:
                for (account, buyer), _ts in round_targets:
                    if self._stop.is_set():
                        break
                    try:
                        self.pddbridge_source.pull_history(buyer, account, size=size,
                                                           timeout=pull_timeout)
                        pulled += 1
                    except Exception as exc:
                        failed += 1
                        log.debug("history pull failed (%s/%s): %s", account, buyer, exc)
                    self._stop.wait(gap)
            else:
                from concurrent.futures import ThreadPoolExecutor

                def _one(item):
                    (account, buyer), _ts = item
                    try:
                        self.pddbridge_source.pull_history(buyer, account, size=size,
                                                           timeout=pull_timeout)
                        return True
                    except Exception as exc:
                        log.debug("history pull failed (%s/%s): %s", account, buyer, exc)
                        return False

                # 并行拉：每个会话在自己的店铺 worker 上执行，互不阻塞（0.7.9 逐店隔离）
                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="history-pull") as pool:
                    for ok in pool.map(_one, round_targets):
                        if self._stop.is_set():
                            break
                        if ok:
                            pulled += 1
                        else:
                            failed += 1
            elapsed = time.monotonic() - started
            self._history_pull_stats = {
                "rounds": getattr(self, "_history_pull_stats", {}).get("rounds", 0) + 1,
                "targets": len(ordered),
                "attempted": len(round_targets),
                "pulled": pulled,
                "failed": failed,
                "last_round_seconds": round(elapsed, 2),
            }
            if pulled or failed:
                log.info("history pull: %d/%d backfilled, %d failed, %.1fs (targets=%d cursor=%d)",
                         pulled, len(round_targets), failed, elapsed, len(ordered), cursor)
            self._save_history_targets()

    def _command_loop(self) -> None:
        # 中心若不在长轮询里提前返回，20s 的等待会让 AI 回复排名延迟几十秒
        # （实测“创建→执行”为 4.6s / 30.8s / 48.7s，也是自动命令被判过期的深层原因）。
        # 用配置的 command_poll_seconds（默认 1.5s）并收敛到 0.5–5s，兼顾时延与请求量。
        try:
            poll = float(self.cfg.get("command_poll_seconds") or 1.5)
        except (TypeError, ValueError):
            poll = 1.5
        wait = max(0.5, min(poll, 5.0))
        self._ensure_result_reporter()
        self._start_sender_pool()
        while not self._stop.is_set():
            self._handle_commands(wait_seconds=wait)
            self._stop.wait(0.2)

    def _heartbeat(self) -> None:
        status = self._status_payload()
        self._last_status = dict(status)
        with self._pending_lock:
            metrics = queue_metrics(self._pending)
            hold = self._queue_hold(self._pending, time.time())
        status["event_queue"] = {**metrics, **hold}
        cdp = {}
        try:
            src = getattr(self, "pddbridge_source", None)
            if src is not None and hasattr(src, "eval_stats"):
                cdp = src.eval_stats() or {}
        except Exception:
            cdp = {}
        if cdp:
            log.info("event queue pending=%d oldest_wait=%.3fs capture_delay_max=%.3fs history=%d"
                     " | cdp evals=%d slow=%d max=%dms sessions=%d idle_poll=%.1fs",
                     metrics["pending"], metrics["oldest_wait_seconds"],
                     metrics["max_capture_delay_seconds"], metrics["history_pending"],
                     cdp.get("evals", 0), cdp.get("slow", 0), cdp.get("max_ms", 0),
                     cdp.get("sessions", 0), cdp.get("idle_poll", 0.0))
        else:
            log.info("event queue pending=%d oldest_wait=%.3fs capture_delay_max=%.3fs history=%d",
                     metrics["pending"], metrics["oldest_wait_seconds"],
                     metrics["max_capture_delay_seconds"], metrics["history_pending"])
        # 卡住时必须给原因：是等历史门槛、在途、重试，还是等本地优先队列
        if hold["held"] and hold["oldest_wait_seconds"] >= 30.0:
            log.warning("event queue hold %d 条未上送 reason=%s 最老等 %.1fs"
                        "（门槛 %.0fs，最早一条还要 %.1fs）",
                        hold["held"], hold["reason"], hold["oldest_wait_seconds"],
                        hold["window_seconds"], hold["min_remaining_seconds"])
        else:
            log.debug("event queue hold held=%d reason=%s min_remaining=%.1fs",
                      hold["held"], hold["reason"], hold["min_remaining_seconds"])
        # 采集看门狗：注入了但一直零帧，界面不能显示“正常”。
        capture = self._capture_status(
            self.pddbridge_source.status() if self._effective_source == "cdp" and self.pddbridge_source else None)
        if not capture.get("ok"):
            now = time.time()
            try:
                warn_every = max(60.0, float(self.cfg.get("capture_warn_seconds") or 600.0))
            except (TypeError, ValueError):
                warn_every = 600.0
            if now - getattr(self, "_capture_warned_at", 0.0) >= warn_every:
                self._capture_warned_at = now
                log.warning("采集告警: 没有收到任何消息（%s）—— %s",
                            capture.get("reason"), capture.get("detail") or "")
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
        # 现场最常见的"无法就绪"其实是代理环境变量把 127.0.0.1 也代理了：
        # 本机探活已统一走 bridge.netlocal（强制免代理），这里只做可见性提示。
        try:
            from .netlocal import active_proxy_env

            proxy = active_proxy_env()
            if proxy:
                log.warning("检测到代理环境变量 %s；本机回环（CDP/网关/直发管道）已强制绕代理",
                            ", ".join(sorted(proxy)))
        except Exception:
            pass
        log.info(
            "%s %s 启动  工位=%s  服务器=%s",
            __product__,
            __version__,
            self.cfg.get("agent_name") or self.cfg.get("agent_id"),
            self.cfg.get("server_url"),
        )
        if not self.cfg.get("agent_token"):
            raise SystemExit(
                "agent_token 未配置。请编辑配置文件或设置环境变量 BRIDGE_AGENT_TOKEN"
            )
        try:
            reg = self.client.register()
            self._apply_registered_agent_id(reg)
            log.info("已连接中心，注册成功（%s）", self.platform.label)
            log.debug("register detail: %s", reg)
        except BridgeClientError as exc:
            log.warning("暂时连不上中心，将自动重试：%s", exc)
            self._last_error = f"register: {exc}"

        if bool(self.cfg.get("websocket_enabled", False)):
            try:
                self.client.connect_websocket()
                log.info("websocket ready connection established")
            except BridgeClientError as exc:
                self._last_error = f"websocket: {exc}"
                log.warning("websocket connect failed; HTTP fallback remains active: %s", exc)

        self._start_feeds()
        if self.platform.name == "pdd" and self.pddbridge_source is not None:
            try:
                pull_seconds = float(self.cfg.get("history_pull_seconds") or 0)
            except (TypeError, ValueError):
                pull_seconds = 0
            if pull_seconds > 0:
                try:
                    self.pddbridge_source.start()
                except Exception as exc:
                    log.warning("CDP 历史补拉启动失败(沿用探域日志): %s", exc)
                else:
                    log.info("已开启 CDP 历史补拉：每 %.0fs 对最近 %s 个会话回拉历史",
                             pull_seconds, self.cfg.get("history_pull_max_buyers") or 40)
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
                wakeup = getattr(self, "_events_wakeup", None)
                if wakeup is not None:
                    triggered = wakeup.wait(timeout=0.2)
                    if triggered:
                        wakeup.clear()
                    if self._stop.is_set():
                        break
                    if triggered:
                        self._schedule_event_flush()
                else:
                    self._stop.wait(0.2)
                now = time.time()
                if now - last_hb >= hb_every:
                    self._heartbeat()
                    self._schedule_event_flush()
                    last_hb = now
                self._stop.wait(0.2)
        finally:
            self._stop.set()
            self.watcher.stop()
            command_thread.join(timeout=1.0)
            self._upload_executor.shutdown(wait=False, cancel_futures=True)
            try:
                self.client.close()
            except Exception:
                pass
            log.info("桥接已停止")

    def stop(self) -> None:
        self._stop.set()
        try:
            self.watcher.stop()
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
    parser.add_argument("--platform", default="", help="pdd")
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
