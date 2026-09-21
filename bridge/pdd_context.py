# -*- coding: utf-8 -*-
"""Read recent buyer order context from the PDD workbench CDP page."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List
from urllib import request as urlrequest

import psutil


SOURCE = "pdd_cdp_order_context"
LOOKUP_SCOPE = "buyer_shop_recent_orders"
_DEBUG_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")


def mall_id_from_account(account: Any) -> str:
    """Extract only the explicit mall component from a canonical PDD account."""
    text = str(account or "").strip()
    match = re.match(r"^cs_(\d+)(?::|_)", text)
    return match.group(1) if match else ""


def _clean_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _price_from_cents(value: Any) -> tuple[Any, Any]:
    if isinstance(value, bool) or value in (None, ""):
        return "", ""
    if isinstance(value, (int, float)):
        display = f"{float(value) / 100:.2f}".rstrip("0").rstrip(".")
        return display, value
    return _clean_text(value, 100), ""


def _failure(code: str) -> dict:
    return {
        "local_context_lookup": {
            "ok": False,
            "source": SOURCE,
            "error": _clean_text(code, 80) or "lookup_failed",
        }
    }


def _goods_rows(value: Any) -> List[dict]:
    values = value if isinstance(value, list) else [value]
    rows: List[dict] = []
    for raw in values[:5]:
        if not isinstance(raw, dict):
            continue
        raw_price = raw.get("goods_price") if raw.get("goods_price") is not None else raw.get("goodsPrice")
        goods_price, goods_price_cents = _price_from_cents(raw_price)
        row = {
            "goods_id": _clean_text(raw.get("goods_id") or raw.get("goodsId"), 64),
            "goods_name": _clean_text(raw.get("goods_name") or raw.get("goodsName"), 500),
            "goods_thumb_url": _clean_text(
                raw.get("goods_thumb_url") or raw.get("thumbUrl") or raw.get("thumb_url"),
                1500,
            ),
            "goods_spec": _clean_text(raw.get("goods_spec") or raw.get("spec"), 500),
            "goods_price": goods_price,
            "goods_price_cents": goods_price_cents,
            "goods_number": raw.get("goods_number") if raw.get("goods_number") is not None else raw.get("goodsNumber"),
        }
        rows.append({key: item for key, item in row.items() if item not in (None, "")})
    return rows


def normalize_order_response(mall_id: Any, response: Any) -> dict:
    """Convert a sanitized CDP response into the bridge upload contract."""
    expected_mall = _clean_text(mall_id, 32)
    if not expected_mall or not isinstance(response, dict) or not response.get("ok"):
        return _failure("invalid_response")

    raw_orders = response.get("orders")
    if raw_orders is None:
        raw_orders = []
    if not isinstance(raw_orders, list):
        return _failure("invalid_orders")

    try:
        raw_total = max(0, int(response.get("total") or 0))
    except (TypeError, ValueError):
        return _failure("invalid_total")

    matched: List[dict] = []
    mismatched = 0
    for raw in raw_orders:
        if not isinstance(raw, dict):
            continue
        order_mall = _clean_text(raw.get("mall_id") or raw.get("mallId"), 32)
        if order_mall != expected_mall:
            mismatched += 1
            continue
        order_id = _clean_text(
            raw.get("order_id") or raw.get("orderSn") or raw.get("order_sn"),
            128,
        )
        if not order_id:
            return _failure("missing_visible_order_id")
        goods = _goods_rows(raw.get("goods") or raw.get("orderGoodsList"))
        raw_amount = raw.get("order_amount") if raw.get("order_amount") is not None else raw.get("orderAmount")
        order_amount, order_amount_cents = _price_from_cents(raw_amount)
        summary = {
            "order_id": order_id,
            "mall_id": expected_mall,
            "created_at": raw.get("created_at") if raw.get("created_at") is not None else raw.get("createdAt"),
            "order_status": raw.get("order_status") if raw.get("order_status") is not None else raw.get("orderStatus"),
            "pay_status": raw.get("pay_status") if raw.get("pay_status") is not None else raw.get("payStatus"),
            "shipping_status": raw.get("shipping_status") if raw.get("shipping_status") is not None else raw.get("shippingStatus"),
            "order_amount": order_amount,
            "order_amount_cents": order_amount_cents,
            "goods": goods,
        }
        matched.append({key: value for key, value in summary.items() if value not in (None, "", [])})

    if mismatched and not matched and raw_total:
        return _failure("mall_filter_mismatch")

    # userAllOrder is executed inside a mall-authenticated page. If every row
    # passed the explicit mallId check, its total also belongs to that mall.
    total_count = raw_total if not mismatched else len(matched)
    total_count = max(total_count, len(matched))
    base_info: Dict[str, Any] = {
        "source": SOURCE,
        "context_received": True,
        "order_count": total_count,
        "total_order_count": total_count,
        "ambiguous": total_count > 1,
        "lookup_scope": LOOKUP_SCOPE,
        "mall_id": expected_mall,
    }
    local_lookup = {
        "ok": True,
        "source": SOURCE,
        "order_count": total_count,
    }

    if total_count == 0:
        base_info["no_orders"] = True
        return {
            "order_id": "",
            "order_info": base_info,
            "local_context_lookup": local_lookup,
        }

    base_info["orders"] = matched[:10]
    result: Dict[str, Any] = {
        "order_info": base_info,
        "local_context_lookup": local_lookup,
    }
    if total_count == 1 and len(matched) == 1:
        selected = matched[0]
        result["order_id"] = selected["order_id"]
        for key in (
            "created_at", "order_status", "pay_status", "shipping_status",
            "order_amount", "order_amount_cents",
        ):
            if key in selected:
                base_info[key] = selected[key]
        goods = selected.get("goods") if isinstance(selected.get("goods"), list) else []
        if goods:
            first_goods = goods[0]
            for key in ("goods_id", "goods_name", "goods_thumb_url", "goods_spec", "goods_price"):
                if key in first_goods:
                    result[key] = first_goods[key]
                    base_info[key] = first_goods[key]
    return result


def merge_lookup_result(message: dict, result: dict) -> dict:
    """Merge lookup data without discarding context already carried by the message."""
    merged = dict(message)
    lookup = result.get("local_context_lookup") if isinstance(result, dict) else None
    if not isinstance(lookup, dict):
        lookup = _failure("invalid_lookup_result")["local_context_lookup"]
    merged["local_context_lookup"] = dict(lookup)
    if not lookup.get("ok"):
        return merged

    incoming_info = result.get("order_info") if isinstance(result.get("order_info"), dict) else {}
    existing_info = merged.get("order_info") if isinstance(merged.get("order_info"), dict) else {}
    existing_order_id = _clean_text(merged.get("order_id"), 128)
    incoming_count = int(incoming_info.get("order_count") or 0)

    # An explicit order card in the buyer message is authoritative even when it
    # falls outside the recent-order window used by the workbench endpoint.
    if incoming_count == 0 and existing_order_id:
        preserved = dict(existing_info)
        preserved.setdefault("context_received", True)
        preserved.setdefault("order_count", 1)
        preserved.setdefault("ambiguous", False)
        preserved["recent_order_lookup"] = dict(incoming_info)
        merged["order_info"] = preserved
        return merged

    if incoming_info:
        combined = dict(existing_info)
        if existing_info and existing_info.get("source") != incoming_info.get("source"):
            combined["message_order_context"] = dict(existing_info)
        combined.update(incoming_info)
        merged["order_info"] = combined
    if "order_id" in result:
        merged["order_id"] = result.get("order_id") or ""
    for key in ("goods_id", "goods_name", "goods_url", "goods_thumb_url", "goods_price", "goods_spec"):
        value = result.get(key)
        # The product explicitly carried by the buyer's message is the product
        # being consulted. A recent order may add order context, but must not
        # replace that product with an older purchased item.
        if value not in (None, "") and merged.get(key) in (None, ""):
            merged[key] = value
    return merged


class PddOrderContextLookup:
    """Small synchronous lookup used from the bridge's order-context worker pool."""

    def __init__(
        self,
        *,
        timeout: float = 1.8,
        cache_seconds: float = 25.0,
        cache_max_entries: int = 2000,
        process_iter: Callable[..., Iterable[Any]] | None = None,
    ) -> None:
        self.timeout = max(0.3, float(timeout))
        # 同一会话重复查询是最大的浪费：买家连发 10 条、日志与 CDP 两路各送一次，
        # 都靠这份结果缓存去掉。订单/商品几十秒内基本不变，TTL 到期自然刷新。
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._failure_cache_seconds = min(2.0, self.cache_seconds)
        self._cache_max_entries = max(16, int(cache_max_entries))
        self._result_cache: Dict[tuple, tuple[float, dict]] = {}
        self._process_iter = process_iter or psutil.process_iter
        self._lock = threading.RLock()
        self._target_cache: Dict[str, tuple[float, str]] = {}

    def _debug_ports(self) -> List[int]:
        ports: set[int] = set()
        try:
            processes = self._process_iter(["name", "cmdline"])
        except TypeError:
            processes = self._process_iter()
        for process in processes:
            try:
                info = getattr(process, "info", {}) or {}
                name = str(info.get("name") or process.name() or "").lower()
                if "pdd" not in name or "workbench" not in name:
                    continue
                cmdline = info.get("cmdline") or process.cmdline() or []
                for index, argument in enumerate(cmdline):
                    text = str(argument or "")
                    match = _DEBUG_PORT_RE.match(text)
                    raw_port = match.group(1) if match else ""
                    if match and not raw_port and index + 1 < len(cmdline):
                        raw_port = str(cmdline[index + 1] or "")
                    if raw_port.isdigit() and 1024 <= int(raw_port) <= 65535:
                        ports.add(int(raw_port))
            except (psutil.Error, OSError, ValueError, AttributeError):
                continue
        return sorted(ports)

    def _right_panel_targets(self, deadline: float) -> List[str]:
        targets: List[str] = []
        for port in self._debug_ports()[:4]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with urlrequest.urlopen(
                    f"http://127.0.0.1:{port}/json/list",
                    timeout=min(remaining, 0.8),
                ) as response:
                    rows = json.loads(response.read().decode("utf-8"))
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or "right_panel" not in str(row.get("url") or ""):
                    continue
                websocket_url = str(row.get("webSocketDebuggerUrl") or "").strip()
                if websocket_url:
                    targets.append(websocket_url)
        return targets

    def _evaluate(self, websocket_url: str, expression: str, deadline: float) -> dict:
        import websocket  # type: ignore

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("context_deadline_exceeded")
        connection = websocket.create_connection(
            websocket_url,
            timeout=remaining,
            suppress_origin=True,
        )
        try:
            request_id = int(time.time_ns() % 1_000_000_000)
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.send(json.dumps({
                "id": request_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }))
            while time.monotonic() < deadline:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                packet = json.loads(connection.recv())
                if packet.get("id") != request_id:
                    continue
                if packet.get("error"):
                    raise RuntimeError("cdp_protocol_error")
                remote = packet.get("result", {}).get("result", {})
                if remote.get("subtype") == "error" or packet.get("result", {}).get("exceptionDetails"):
                    raise RuntimeError("cdp_javascript_error")
                value = remote.get("value")
                if not isinstance(value, dict):
                    raise RuntimeError("cdp_invalid_value")
                return value
            raise TimeoutError("cdp_timeout")
        finally:
            connection.close()

    @staticmethod
    def _query_expression(mall_id: str, buyer_id: str) -> str:
        expected = json.dumps(mall_id)
        buyer = json.dumps(buyer_id)
        return f"""(async()=>{{
const expectedMall={expected};
const buyerId={buyer};
const parse=(value)=>{{try{{return JSON.parse(value||'{{}}')}}catch(_){{return {{}}}}}};
const info=parse(localStorage.getItem('userinfo'));
const newer=parse(localStorage.getItem('new_userinfo'));
const pageMall=String(info.mall_id||info.mallId||newer.mall_id||newer.mallId||'');
if(pageMall!==expectedMall)return {{skip:true,mall_id:pageMall}};
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
// 等面板就绪 (.main.__vue__.$axios 可用), 最多 16 次 * 200ms = 3.2s
let root=null;
for(let i=0;i<16;i++){{
  root=document.querySelector('.main')&&document.querySelector('.main').__vue__;
  if(root&&root.$axios&&root.$axios.post)break;
  await sleep(200);
}}
if(!root||!root.$axios)return {{ok:false,error:'panel_not_ready',mall_id:pageMall}};
const fetchOrders=async()=>{{
  try{{
    const response=await root.$axios.post('/latitude/order/userAllOrder',{{pageNo:1,pageSize:20,uid:buyerId}});
    const rows=Array.isArray(response&&response.orders)?response.orders:[];
    const orders=rows.map(order=>{{
      const rawGoods=Array.isArray(order.orderGoodsList)?order.orderGoodsList:(order.orderGoodsList?[order.orderGoodsList]:[]);
      const goods=rawGoods.slice(0,5).map(item=>({{
        goods_id:String(item.goodsId||item.goods_id||''),
        goods_name:String(item.goodsName||item.goods_name||''),
        goods_thumb_url:String(item.thumbUrl||item.goods_thumb_url||''),
        goods_spec:String(item.spec||item.goods_spec||''),
        goods_price:item.goodsPrice==null?item.goods_price:item.goodsPrice,
        goods_number:item.goodsNumber==null?item.goods_number:item.goodsNumber
      }}));
      return {{
        order_id:String(order.orderSn||order.order_sn||order.orderId||''),
        mall_id:String(order.mallId||order.mall_id||''),
        created_at:order.createdAt,
        order_status:order.orderStatus,
        pay_status:order.payStatus,
        shipping_status:order.shippingStatus,
        order_amount:order.orderAmount,
        goods
      }};
    }});
    return {{ok:true,mall_id:pageMall,total:Number(response&&response.total||0),orders}};
  }}catch(_){{return {{ok:false,error:'request_failed',mall_id:pageMall}}}};
}};
// 面板刚就绪时首次查询可能是空响应(时序), 重试 3 次, 间隔 350ms
let last=null;
for(let i=0;i<3;i++){{
  last=await fetchOrders();
  if(last.ok && (last.orders&&last.orders.length||last.total>0))return last;
  if(last.ok && !last.orders && last.total===0){{ await sleep(350); continue; }}
  return last;
}}
return last;
}})()"""

    def lookup(self, message: dict) -> dict:
        """带会话级结果缓存：同一 (店铺, 买家) 在 TTL 内只真正查一次 CDP。"""
        mall_id = mall_id_from_account(message.get("account"))
        buyer_id = _clean_text(message.get("buyer_id"), 128)
        if not mall_id:
            return _failure("account_mall_missing")
        if not buyer_id:
            return _failure("buyer_id_missing")
        cache_key = (mall_id, buyer_id)
        with self._lock:
            hit = self._result_cache.get(cache_key)
            if hit and hit[0] > time.monotonic():
                return dict(hit[1])
        result = self._lookup_uncached(mall_id, buyer_id)
        lookup = result.get("local_context_lookup") if isinstance(result, dict) else None
        ttl = self.cache_seconds if isinstance(lookup, dict) and lookup.get("ok") else self._failure_cache_seconds
        if ttl > 0:
            with self._lock:
                self._result_cache[cache_key] = (time.monotonic() + ttl, dict(result))
                if len(self._result_cache) > self._cache_max_entries:
                    self._prune_result_cache()
        return result

    def _prune_result_cache(self) -> None:
        now = time.monotonic()
        for key in [key for key, (expires, _value) in self._result_cache.items() if expires <= now]:
            self._result_cache.pop(key, None)
        while len(self._result_cache) > self._cache_max_entries:
            self._result_cache.pop(next(iter(self._result_cache)))

    def _lookup_uncached(self, mall_id: str, buyer_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        expression = self._query_expression(mall_id, buyer_id)
        targets: List[str] = []
        # 锁只保护目标探测/缓存：Runtime.evaluate 的网络往返必须能并行，
        # 否则加再多 worker 也只是一条串行队列（100+/分钟就压死）。
        with self._lock:
            cached = self._target_cache.get(mall_id)
            if cached and cached[0] > time.monotonic():
                targets.append(cached[1])
            if not targets:
                targets.extend(self._right_panel_targets(deadline))
        if not targets:
            return _failure("pdd_cdp_unavailable")

        saw_matching_panel = False
        for target in targets:
            if time.monotonic() >= deadline:
                return _failure("context_deadline_exceeded")
            try:
                response = self._evaluate(target, expression, deadline)
            except Exception:
                continue
            if response.get("skip"):
                continue
            saw_matching_panel = True
            if not response.get("ok"):
                return _failure(str(response.get("error") or "request_failed"))
            with self._lock:
                self._target_cache[mall_id] = (time.monotonic() + 10.0, target)
            return normalize_order_response(mall_id, response)
        with self._lock:
            self._target_cache.pop(mall_id, None)
        return _failure("lookup_failed" if saw_matching_panel else "mall_panel_not_found")
