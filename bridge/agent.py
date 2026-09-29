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

from . import __build_hash__, __version__
from .client import BridgeClient, BridgeClientError
from .command_journal import CommandJournal
from .event_queue import append_event
from .config import load_config, save_config, write_example_config
from .platforms import get_platform
from .watcher import LogWatcher
from .message_timing import (TIMING_FIELDS, FUTURE_TOLERANCE_SECONDS,
                             TAKEOVER_PARENT_MAX_AGE_SECONDS, epoch_seconds,
                             stamp_message, history_ready, command_expired, queue_metrics)

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
        if account and msg.get("buyer_id") and not msg.get("is_history") and not msg.get("is_diagnostic"):
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
        for cmd in commands:
            command_id = str(cmd.get("id") or cmd.get("command_id") or "").strip()
            if not command_id:
                log.warning("command pull returned item without id")
                continue
            existing = self.command_journal.get(command_id)
            if existing is not None:
                if existing.get("state") == "result_pending":
                    log.info("command %s duplicate; result_pending, retrying ACK", command_id)
                    self._report_command_result(command_id, dict(existing.get("result") or {}))
                else:
                    log.info("command %s duplicate; state=%s, skip execution", command_id,
                             existing.get("state"))
                continue
            if command_id in self._commands_done:
                continue
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
                continue
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
                duration = time.monotonic() - started
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
                log.error("command %s result journal write failed: %s", command_id,
                          self.command_journal.last_error)
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
        while not self._stop.is_set():
            self._handle_commands(wait_seconds=wait)
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
