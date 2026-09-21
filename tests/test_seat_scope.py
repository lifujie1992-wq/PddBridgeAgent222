# -*- coding: utf-8 -*-
"""本机席位过滤（不串台）。

实测背景（2026-09-21）：本机探域客户端替**别的电脑登录的店铺代发消息**，本机日志
目录里因此混进 9 个店铺账号 —— `mall_100000001` 一个店铺就横跨 **7 个席位**，而本机
工作台只打开了 2 个（`cs_100000003:200000001`、`cs_100000005:200000012`）。
桥接原先不做归属校验，全部照收 → 浮窗串台。

**这个文件的核心是 `test_same_shop_different_seat_is_blocked`**：
按店铺 id 过滤是不够的，必须精确到席位。
"""
import logging
import time

import pytest

from bridge.agent import _same_seat
from bridge.seat_scope import (SCOPE_ALLOW, SCOPE_BLOCK, SCOPE_UNKNOWN, SeatScope,
                               seat_key)

# 本机工作台实际打开的席位（现场值）
LOCAL_A = "cs_100000003:200000001"
LOCAL_B = "cs_100000005:200000012"
# 别的电脑登录的席位：**同一个店铺** mall_100000001，但不是本机这个席位
OTHER_SHOP = "mall_100000001"
OTHER_SEAT = "cs_100000001:200000007"
LOCAL_SEAT_OF_THAT_SHOP = "cs_100000001:200000008"


def scope_with(*accounts, **cfg):
    sc = SeatScope(cfg)
    sc.refresh(list(accounts))
    return sc


# ==================================================== 1. 核心：席位粒度

def test_same_shop_different_seat_is_blocked():
    """**核心回归**：同一个店铺下，别的席位必须拦下。

    这正是"按店铺 id 过滤不够"的证据 —— 两个账号共享店铺号 100000001，
    只有席位 uid 不同。
    """
    sc = scope_with(LOCAL_SEAT_OF_THAT_SHOP, LOCAL_A)
    assert sc.allows(LOCAL_SEAT_OF_THAT_SHOP, "mall_cs") == SCOPE_ALLOW
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_BLOCK, \
        "同店铺不同席位必须拦下（按店铺 id 过滤会放过它）"
    assert sc.stats["blocked_other_seat"] == 1


def test_same_seat_in_underscore_form_is_allowed():
    """日志里席位写成下划线（`cs_100000003_200000001`），必须认成本机。"""
    sc = scope_with(LOCAL_A)
    assert sc.allows("cs_100000003_200000001", "mall_cs") == SCOPE_ALLOW
    assert sc.allows("cs-100000003-200000001", "mall_cs") == SCOPE_ALLOW


def test_the_other_seats_of_that_shop_are_all_blocked():
    """现场那 7 个别家席位，一个都不能过。"""
    others = ["cs_100000001:200000003", "cs_100000001:200000006", "cs_100000001:200000004",
              "cs_100000001:200000002", "cs_100000001:200000005", "cs_100000001:200000007",
              "cs_100000001:200000009"]
    sc = scope_with(LOCAL_A, LOCAL_B)
    for account in others:
        assert sc.allows(account, "mall_cs") == SCOPE_BLOCK, "%s 不该通过" % account
    assert sc.allows(LOCAL_A, "mall_cs") == SCOPE_ALLOW
    assert sc.allows(LOCAL_B, "mall_cs") == SCOPE_ALLOW


# ==================================================== 2. 认不出归属 → 拦下

@pytest.mark.parametrize("bad", ["", None, "   ", "100000003", "pdd42730237415", "不是账号"])
def test_unrecognizable_account_is_blocked(bad):
    """认不出归属的一律拦下（这些正是"串台"的主要载体）。"""
    sc = scope_with(LOCAL_A)
    assert sc.allows(bad, "mall_cs") == SCOPE_BLOCK


def test_shop_only_account_is_blocked():
    """只认得出店铺、认不出席位 → 拦下：区分不了本机席位和别家席位。"""
    sc = scope_with(LOCAL_A)
    assert sc.allows(OTHER_SHOP, "mall_cs") == SCOPE_BLOCK
    assert sc.stats["blocked_shop_only"] == 1
    assert sc.allows(LOCAL_SEAT_OF_THAT_SHOP, "mall_cs") == SCOPE_BLOCK  # 本机没开这个店铺
    assert sc.allows("cs_100000003:999999", "mall_cs") == SCOPE_BLOCK    # 同店铺别的席位


def test_fabricated_seat_zero_is_blocked():
    """`_message_with_local_context` 会造出 `cs_<mall>:0` 这种假席位 —— 必须拦下。"""
    sc = scope_with(LOCAL_A)
    assert sc.allows("cs_100000001:0", "mall_cs") == SCOPE_BLOCK


# ==================================================== 3. 探测不到 → fail-open

def test_empty_scope_fails_open_with_unknown():
    """席位集合探测不到（工作台没开）→ UNKNOWN，调用方放行 + 告警。

    过滤失灵绝不能变成"全店停止服务"。
    """
    sc = SeatScope({}, accounts_provider=lambda: [])
    sc.refresh()
    assert sc.allows(LOCAL_A, "mall_cs") == SCOPE_UNKNOWN
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_UNKNOWN
    assert sc.stats["unknown_passed"] == 2
    assert sc.stats["blocked"] == 0


def test_provider_failure_keeps_the_previous_set():
    """provider 抛异常时沿用上一次的集合，不能把集合清空（否则等于放行全部）。"""
    sc = scope_with(LOCAL_A)
    sc._provider = lambda: (_ for _ in ()).throw(RuntimeError("CDP 挂了"))
    sc._refreshed_at = 0.0
    assert sc.allows(LOCAL_A, "mall_cs") == SCOPE_ALLOW
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_BLOCK


# ==================================================== 4. 可回退 / 可覆盖

def test_switch_off_allows_everything():
    """`enforce_seat_scope=false` 一键退回现状（出问题时的回退手段）。"""
    sc = scope_with(LOCAL_A, enforce_seat_scope=False)
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_ALLOW
    assert sc.allows("", "mall_cs") == SCOPE_ALLOW


def test_config_allowlist_is_additive():
    """配置白名单是**追加**，不是替代 —— 供"工作台没开但也要服务"的场景兜底。"""
    sc = scope_with(LOCAL_A, allowed_seat_accounts=[OTHER_SEAT],
                    allowed_shop_ids=["mall_100000001"])
    assert sc.allows(LOCAL_A, "mall_cs") == SCOPE_ALLOW          # 自动发现的仍在
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_ALLOW       # 精确追加的
    assert sc.allows("cs_100000001:200000009", "mall_cs") == SCOPE_ALLOW  # 整店放行


# ==================================================== 5. 收缩告警

def test_shrink_warns_loudly(caplog):
    """席位集合收缩必须告警：被关掉的标签页对应的店铺会被拦下，买家收不到回复。"""
    sc = scope_with(LOCAL_A, LOCAL_B)
    with caplog.at_level(logging.WARNING, logger="pdd.bridge"):
        sc.refresh([LOCAL_A])
    assert sc.stats["shrink_warnings"] == 1
    assert any("席位集合收缩" in r.getMessage() for r in caplog.records), \
        "收缩时必须留一行告警"
    assert sc.allows(LOCAL_B, "mall_cs") == SCOPE_BLOCK


def test_growth_does_not_warn(caplog):
    """扩大是正常的（多开一个店铺标签页），不该刷告警。"""
    sc = scope_with(LOCAL_A)
    with caplog.at_level(logging.WARNING, logger="pdd.bridge"):
        sc.refresh([LOCAL_A, LOCAL_B])
    assert sc.stats["shrink_warnings"] == 0


# ==================================================== 6. 可观测性

def test_status_exposes_seats_and_counters():
    """线上要能一眼看出"过滤在不在工作、本机认成了哪几个席位"。"""
    sc = scope_with(LOCAL_A, LOCAL_B)
    sc.allows(LOCAL_A, "mall_cs")
    sc.allows(OTHER_SEAT, "mall_cs")
    st = sc.status()
    assert sorted(st["seats"]) == sorted(["cs_100000003:200000001", "cs_100000005:200000012"])
    assert st["allowed"] == 1 and st["blocked"] == 1
    assert "不是本机席位" in st["last_blocked"]
    assert st["enabled"] is True


# ==================================================== 7. 为什么不能用 _same_seat

def test_why_we_do_not_reuse_same_seat():
    """把"为什么另写 seat_key"钉成测试。

    `agent._same_seat()` 按"6 位以上数字求交集"判断，共享店铺号的**不同席位会被它
    判成同一个** —— 拿它做本机过滤，`cs_100000001:200000007`（别家）会被误判成
    `cs_100000001:200000008`（本机）而放行，等于没过滤。
    """
    assert _same_seat(OTHER_SEAT, LOCAL_SEAT_OF_THAT_SHOP) is True, \
        "确认 _same_seat 确实会把同店铺的不同席位判成同一个（所以不能复用它）"
    assert seat_key(OTHER_SEAT) != seat_key(LOCAL_SEAT_OF_THAT_SHOP), \
        "seat_key 必须能区分同店铺的不同席位"


# ==================================================== 8. 入站永不设限（生产事故修复）

def test_inbound_buyer_message_is_never_blocked():
    """**生产事故回归**：买家进线消息**永远**不能被过滤器拦掉。

    2026-09-21 真实机器上的事故：多多客户端收到了买家消息，但浮窗不显示，
    必须人工回复后才出现，导致超时。原因是买家消息解析出的席位不在
    "CDP 探测到的本机席位集合"里，被判成"不是本机"拦下了。

    依据（实测）：本机探域日志里**买家进线帧 18/18 全是本机店铺的**，
    串台混进来的**全是出站记录**（`Send_Robot_Msg`）。所以入站根本不需要过滤。
    """
    sc = scope_with(LOCAL_A)          # 席位集合里**没有**别的店铺
    for role in ("user", "buyer", "", None, "USER"):
        assert sc.allows(OTHER_SEAT, role) == SCOPE_ALLOW, \
            "入站消息（role=%r）绝不能被拦" % (role,)
    assert sc.allows("", "user") == SCOPE_ALLOW          # 认不出归属的入站也放行
    assert sc.allows("cs_999:1", "user") == SCOPE_ALLOW


def test_inbound_passes_even_when_seat_set_is_populated():
    """席位集合非空时，入站照样放行 —— 不依赖席位判定。"""
    sc = scope_with(LOCAL_A, LOCAL_B)
    assert sc.allows("cs_100000001:200000007", "user") == SCOPE_ALLOW
    assert sc.stats["blocked"] == 0, "入站不该产生任何拦截计数"


def test_outbound_is_still_filtered():
    """反面钉子：入站放行不能把出站的过滤一起放开（否则串台又回来了）。"""
    sc = scope_with(LOCAL_A)
    assert sc.allows(OTHER_SEAT, "mall_cs") == SCOPE_BLOCK
    assert sc.allows("cs_100000001:200000007", "mall_cs") == SCOPE_BLOCK
    assert sc.stats["blocked"] == 2
