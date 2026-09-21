# -*- coding: utf-8 -*-
"""本机席位集合：桥接只处理"本机工作台实际登录的席位"的消息，不串台。

## 背景（实测 2026-09-21）

本机的探域客户端会**替别的电脑登录的店铺代发消息**，于是本机探域日志目录
`D:\\kefuAgent\\探域\\tanyu3.0.2\\logs` 里混进了 9 个店铺账号：其中 `mall_100000001`
一个店铺就横跨 **7 个席位**（`200000003` / `200000006` / `200000004` / `200000002` /
`200000005` / `200000007` / `200000009`），而本机工作台实际只打开了 2 个店铺。
桥接原先不做任何归属校验，全部照收 → 浮窗串台。

## 为什么按"席位"而不是"店铺"过滤

`mall_100000001` 一个店铺下有 7 个席位，**只有一个是本机的**。
按店铺 id 过滤会漏掉 6 个别的电脑的席位。
而席位 `cs_<mall>:<seat_uid>` 已经是店铺的细化 —— **一次等值匹配天然两层都管**，
不需要分别做"店铺层"和"席位层"两套判断。

## 判断依据

主来源 = **CDP 会话的 account**（`PddbridgeSource` 的 `sessions[].account`）：
一个 session = 工作台的一个标签页 = `(店铺, 本机登录的那个席位)`。

**注意失效场景**：CDP 只看得见**已打开**的标签页。工作台把某个店铺的标签页关了，
那个店铺的消息就会被拦下（买家收不到回复）。所以：
- 配置里可以追加白名单（`allowed_shop_ids` / `allowed_seat_accounts`）兜底；
- 席位集合**收缩**时大声告警，不静默拦。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .parser import _canonical_account

log = logging.getLogger("pdd.bridge")

# 判定结果。调用方按这三个值分流（老板拍板的边界）：
SCOPE_ALLOW = "allow"        # 是本机席位 → 照常处理
SCOPE_BLOCK = "block"        # 确定不是本机席位，或认不出归属 → 拦下
SCOPE_UNKNOWN = "unknown"    # 本机席位集合探测不到 → 调用方 fail-open（放行 + 告警）

# 默认刷新间隔（秒）。每个事件都去读 CDP 会话会拖慢主链路，所以按 TTL 缓存。
_DEFAULT_REFRESH_SECONDS = 30.0

_FULL_SEAT_RE = re.compile(r"cs_(\d+):(\d+)")
_SHOP_ONLY_RE = re.compile(r"^(?:cs_)?(\d+)$")


def seat_key(value: Any) -> Tuple[str, str]:
    """把各种写法的席位归一成 `(mall_id, seat_uid)`。

    认得出就返回两段；只认得出店铺时 seat_uid 为空串；完全认不出返回 `("", "")`。

    **不要用 `agent._same_seat()` 做这件事**：它按"6 位以上数字求交集"判断，
    会把 `cs_100000001:200000007` 和 `cs_100000001:200000008` 当成同一个席位
    （两者共享店铺号 100000001）—— 而这两个正是要区分开的本机/别家席位。
    """
    text = _canonical_account(str(value or "").strip())
    m = _FULL_SEAT_RE.fullmatch(text)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"mall_(\d+)", text)
    if m:
        return m.group(1), ""
    m = _SHOP_ONLY_RE.fullmatch(text)
    if m:
        return m.group(1), ""
    return "", ""


class SeatScope:
    """本机席位集合 + 判定。线程安全。

    `accounts_provider` 是个零参可调用，返回当前本机在线的席位账号列表
    （生产里接 `PddbridgeSource` 的会话账号）。传 None 表示没有自动发现能力，
    此时只靠配置白名单；两者都为空 → 一律返回 `SCOPE_UNKNOWN`（fail-open）。
    """

    def __init__(self, cfg: Optional[dict] = None,
                 accounts_provider: Optional[Callable[[], Iterable[Any]]] = None) -> None:
        self.cfg = cfg or {}
        self._provider = accounts_provider
        self._lock = threading.Lock()
        self._seats: set[Tuple[str, str]] = set()      # (mall, seat_uid) 精确席位
        self._shops: set[str] = set()                  # 只有店铺号的宽口径
        self._discovered: List[str] = []               # 原始发现值，供日志/状态排查
        self._refreshed_at = 0.0
        self._refresh_seconds = self._num("seat_scope_refresh_seconds",
                                          _DEFAULT_REFRESH_SECONDS, 1.0, 3600.0)
        self.enabled = str(self.cfg.get("enforce_seat_scope", True)).strip().lower() \
            not in {"0", "false", "no", "off"}
        # 计数：线上要能确认它在工作，静默过滤没法排查
        self.stats: Dict[str, Any] = {
            "refreshes": 0, "allowed": 0, "blocked": 0, "unknown_passed": 0,
            "inbound_passed": 0,
            "blocked_empty_account": 0, "blocked_other_seat": 0,
            "blocked_shop_only": 0, "shrink_warnings": 0, "last_blocked": "",
        }

    # ---------------------------------------------------------------- 配置
    def _num(self, key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(self.cfg.get(key) or default)
        except (TypeError, ValueError):
            value = float(default)
        return max(low, min(value, high))

    def _configured_allowlist(self) -> Tuple[set, set]:
        """配置里追加的白名单。`allowed_shop_ids` 是宽口径（整店），
        `allowed_seat_accounts` 是精确到席位。"""
        shops: set[str] = set()
        seats: set[Tuple[str, str]] = set()
        raw_shops = self.cfg.get("allowed_shop_ids") or []
        if isinstance(raw_shops, (list, tuple, set)):
            for item in raw_shops:
                mall, uid = seat_key(item)
                if mall and not uid:
                    shops.add(mall)
                elif mall and uid:
                    seats.add((mall, uid))
        raw_seats = self.cfg.get("allowed_seat_accounts") or []
        if isinstance(raw_seats, (list, tuple, set)):
            for item in raw_seats:
                mall, uid = seat_key(item)
                if mall and uid:
                    seats.add((mall, uid))
        return shops, seats

    # ---------------------------------------------------------------- 刷新
    def refresh(self, accounts: Optional[Iterable[Any]] = None) -> None:
        """重新采集本机席位集合。`accounts=None` 时从 provider 取。"""
        if accounts is None and self._provider is not None:
            try:
                accounts = self._provider()
            except Exception as exc:            # provider 出错不能打挂主链路
                log.warning("席位集合刷新失败（沿用上一次）: %s", exc)
                return
        raw = [str(a or "").strip() for a in (accounts or []) if str(a or "").strip()]
        seats: set[Tuple[str, str]] = set()
        shops: set[str] = set()
        for value in raw:
            mall, uid = seat_key(value)
            if mall and uid:
                seats.add((mall, uid))
            elif mall:
                shops.add(mall)
        cfg_shops, cfg_seats = self._configured_allowlist()
        shops |= cfg_shops
        seats |= cfg_seats
        with self._lock:
            previous = set(self._seats)
            # 收缩才告警：扩大是正常的（多开了一个店铺标签页）
            shrank = previous - seats if not seats.issuperset(previous) else set()
            self._seats = seats
            self._shops = shops
            self._discovered = raw
            self._refreshed_at = time.time()
            self.stats["refreshes"] += 1
            # 集合变了（含**首次建立**）就打一行：seat_scope 在本机网关和中心的状态
            # 接口里都读不到，不打这行就完全没法确认过滤器认了哪几个席位。
            if seats != previous or self.stats["refreshes"] == 1:
                log.info(
                    "本机席位集合：%s%s —— 只有这些席位的消息会推浮窗/上报中心；"
                    "其余（别的电脑登录的店铺/席位）一律拦下并留档到 *_out_of_scope.jsonl",
                    ", ".join("cs_%s:%s" % s for s in sorted(seats)) or "（空，过滤未生效）",
                    "；整店放行 %s" % ", ".join("mall_%s" % m for m in sorted(shops))
                    if shops else "")
            if shrank:
                self.stats["shrink_warnings"] += 1
                log.warning(
                    "席位集合收缩：少了 %s（工作台标签页被关了？这些店铺的消息会被拦下，"
                    "买家将收不到回复。需要继续服务的请加进 allowed_seat_accounts）",
                    ", ".join("cs_%s:%s" % s for s in sorted(shrank)))

    def _ensure_fresh(self, now: float) -> None:
        if now - self._refreshed_at >= self._refresh_seconds:
            self.refresh()

    # ---------------------------------------------------------------- 判定
    def allows(self, account: Any, role: Any = None) -> str:
        """判定一条消息的归属。返回 SCOPE_ALLOW / SCOPE_BLOCK / SCOPE_UNKNOWN。

        判定规则：
        - 关掉开关（`enforce_seat_scope=false`）→ 一律 ALLOW（可一键回退）
        - **入站买家消息（role != "mall_cs"）→ 一律 ALLOW**
          这是 2026-09-21 生产事故的修复：买家消息被过滤器拦掉后浮窗不显示，
          必须人工回复才出现，导致超时。实测本机探域日志里**买家进线帧 18/18
          全是本机店铺的**，而串台混进来的**全是出站记录**（`Send_Robot_Msg`），
          所以入站根本不需要过滤 —— 少拦一条买家消息的代价远大于多一条串台噪音。
        - 席位集合为空（探测不到）→ UNKNOWN，调用方 fail-open
        - 出站：account 为空 / 认不出 → BLOCK
        - 出站：只认得出店铺、认不出席位 → BLOCK（区分不了本机席位和别家）
        - 出站：席位精确命中 → ALLOW；否则 BLOCK
        """
        if not self.enabled:
            return SCOPE_ALLOW
        # 入站买家消息永不设限：这是生产事故的直接修复。
        if str(role or "").strip().lower() != "mall_cs":
            self.stats["inbound_passed"] += 1
            return SCOPE_ALLOW
        now = time.time()
        self._ensure_fresh(now)
        with self._lock:
            seats = set(self._seats)
            shops = set(self._shops)
        if not seats and not shops:
            self.stats["unknown_passed"] += 1
            self.stats["last_blocked"] = ""
            return SCOPE_UNKNOWN
        mall, uid = seat_key(account)
        if not mall:
            self.stats["blocked"] += 1
            self.stats["blocked_empty_account"] += 1
            self.stats["last_blocked"] = "account=%r（认不出归属）" % (account,)
            return SCOPE_BLOCK
        if not uid:
            self.stats["blocked"] += 1
            self.stats["blocked_shop_only"] += 1
            self.stats["last_blocked"] = "account=%r（只认得出店铺，分不清哪个席位）" % (account,)
            return SCOPE_BLOCK
        if (mall, uid) in seats or (mall, "") in seats or mall in shops:
            self.stats["allowed"] += 1
            return SCOPE_ALLOW
        self.stats["blocked"] += 1
        self.stats["blocked_other_seat"] += 1
        self.stats["last_blocked"] = "account=%r（不是本机席位）" % (account,)
        return SCOPE_BLOCK

    # ---------------------------------------------------------------- 观测
    def status(self) -> Dict[str, Any]:
        """进心跳/status。线上要能一眼看出"过滤在不在工作、本机认成了哪几个席位"。"""
        with self._lock:
            seats = sorted(self._seats)
            shops = sorted(self._shops)
            discovered = list(self._discovered)
            refreshed = self._refreshed_at
        return {
            **self.stats,
            "enabled": self.enabled,
            "seats": ["cs_%s:%s" % s for s in seats],
            "shops": ["mall_%s" % s for s in shops],
            "discovered": discovered,
            "refresh_seconds": self._refresh_seconds,
            "age_seconds": round(time.time() - refreshed, 1) if refreshed else None,
        }
