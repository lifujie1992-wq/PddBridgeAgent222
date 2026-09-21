from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest

import run_pdd_client as pdd_client
from run_frontend_service import FrontHandler, LocalSeatState, WEB
from run_pdd_client import (
    LocalDeliveryQueue,
    _gateway_process_matches,
    _message_with_local_context,
    _stop_jump_helpers,
    stop_local_gateway,
)
from bridge.parser import parse_line
from bridge.agent import BridgeAgent


def event(msg_id: str = "message-1") -> dict:
    return {
        "event_id": msg_id,
        "idempotency_key": msg_id,
        "platform": "pdd",
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "msg_id": msg_id,
        "role": "user",
        "content": "local first message",
        "ts": time.time(),
    }


@pytest.fixture
def gateway(tmp_path):
    state = LocalSeatState(tmp_path / "seat_state.json")
    state.configure(
        backend="http://127.0.0.1:1",
        agent_token="local-secret",
        agent_id="pdd-seat-test",
    )
    with state.lock:
        state.active_shop_ids = {"mall_100"}
        state.shop_names = {"mall_100": "Test shop"}
        state._save_locked()

    class Handler(FrontHandler):
        local_state = state
        ui_role = "seat"
        seat_agent_token = "local-secret"
        seat_agent_id = "pdd-seat-test"
        backend_base = "http://127.0.0.1:1"
        web_root = WEB

        def log_message(self, _fmt, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get_json(url: str) -> dict:
    with urlrequest.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def post_event(base_url: str, path: str, payload: dict) -> dict:
    req = urlrequest.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Token": "local-secret",
            "X-Agent-Id": "pdd-seat-test",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_bootstrap(state: LocalSeatState, payload: dict) -> None:
    state._remote_json = lambda path: payload if path == "/api/seat/v1/bootstrap" else {"ok": True}
    state.schedule_refresh("/api/seat/v1/bootstrap")
    deadline = time.time() + 3
    while time.time() < deadline:
        with state.lock:
            if "GET /api/seat/v1/bootstrap" not in state.inflight:
                return
        time.sleep(0.01)
    pytest.fail("bootstrap refresh did not finish")


def bootstrap_shop(*, enabled: bool) -> dict:
    return {
        "ok": True,
        "active_shop_ids": ["mall_100"],
        "shops": [{
            "shop_id": "mall_100",
            "shop_name": "Test shop",
            "account": "cs_100:1",
            "platform": "pdd",
            "status": "active",
            "brain_mode": "own" if enabled else "tanyu",
            "brain_policy_configured": enabled,
            "send_policy": "shop" if enabled else "blocked",
        }],
    }


def test_loopback_workbench_needs_no_user_login(gateway):
    base_url, _state = gateway
    runtime = get_json(base_url + "/api/runtime-config")
    assert runtime["rbac_enabled"] is False
    assert runtime["seat_mode"] is True
    assert get_json(base_url + "/api/auth/me")["authenticated"] is True
    assert get_json(base_url + "/api/sessions?scope=active")["sessions"] == []


def test_session_preview_rerenders_even_while_row_is_hovered() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    refresh = html.split("async function refreshSessions()", 1)[1].split("function renderShopFilter()", 1)[0]
    assert "renderList();" in refresh
    assert "document.querySelector('.sess:hover')" not in refresh


def test_loopback_workbench_serves_bundled_capability_assets(gateway):
    base_url, _state = gateway
    with urlrequest.urlopen(base_url + "/static/capabilities.js", timeout=3) as response:
        script = response.read().decode("utf-8")
    assert response.status == 200
    assert "initCapabilityCenter" in script


def test_dock_control_endpoint_persists_pin_and_adsorb(gateway):
    base_url, state = gateway
    initial = get_json(base_url + "/api/dock/control")
    assert initial["pin"] is True
    assert initial["adsorb"] is True

    changed = post_event(base_url, "/api/dock/control", {"pin": False, "adsorb": False})

    assert changed["pin"] is False
    assert changed["adsorb"] is False
    persisted = json.loads((state.path.parent / "pdd_adsorb_control.json").read_text(encoding="utf-8"))
    assert persisted == {"pin": False, "adsorb": False}


def test_adsorb_queue_merges_local_first_conversations(gateway):
    base_url, _state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("adsorb-local-1"))

    result = get_json(base_url + "/api/queue/handoff?limit=80&handoff=0")

    assert result["ok"] is True
    assert result["local_first"] is True
    assert result["count"] == 1
    assert result["items"][0]["buyer_id"] == "buyer-1"
    assert result["items"][0]["last_text"] == "local first message"
    assert result["items"][0]["ai_takeover_state"] == "shadow"


def test_bootstrap_ai_shop_defaults_local_conversation_to_active(gateway):
    base_url, state = gateway
    refresh_bootstrap(state, bootstrap_shop(enabled=True))
    post_event(base_url, "/api/bridge/v1/events", event("ai-shop-active-1"))

    session = get_json(base_url + "/api/sessions?scope=active")["sessions"][0]
    queued = get_json(base_url + "/api/queue/handoff?limit=80&handoff=0")["items"][0]
    detail = state.merge_detail("/api/session/buyer-1?account=cs_100%3A1")

    for row in (session, queued, detail):
        assert row["shop_ai_takeover_enabled"] is True
        assert row["ai_takeover_enabled"] is True
        assert row["ai_takeover_state"] == "active"

    restored = LocalSeatState(state.path)
    assert restored.shop_ai_takeover == {"mall_100": True}


def test_explicit_conversation_pause_wins_over_enabled_shop(gateway):
    base_url, state = gateway
    refresh_bootstrap(state, bootstrap_shop(enabled=True))
    post_event(base_url, "/api/bridge/v1/events", event("ai-shop-paused-1"))
    with state.lock:
        state.sessions[state._key("cs_100:1", "buyer-1")].update({
            "ai_takeover_override": False,
            "ai_takeover_enabled": False,
            "ai_takeover_state": "paused",
        })

    session = get_json(base_url + "/api/sessions?scope=active")["sessions"][0]
    queued = get_json(base_url + "/api/queue/handoff?limit=80&handoff=0")["items"][0]
    assert session["ai_takeover_state"] == "paused"
    assert session["ai_takeover_enabled"] is False
    assert queued["ai_takeover_state"] == "paused"
    assert queued["ai_takeover_enabled"] is False


def test_bootstrap_non_ai_shop_remains_shadow(gateway):
    base_url, state = gateway
    refresh_bootstrap(state, bootstrap_shop(enabled=False))
    post_event(base_url, "/api/bridge/v1/events", event("non-ai-shop-1"))

    session = get_json(base_url + "/api/sessions?scope=active")["sessions"][0]
    queued = get_json(base_url + "/api/queue/handoff?limit=80&handoff=0")["items"][0]
    assert session["shop_ai_takeover_enabled"] is False
    assert session["ai_takeover_state"] == "shadow"
    assert queued["shop_ai_takeover_enabled"] is False
    assert queued["ai_takeover_state"] == "shadow"


def test_bootstrap_policy_change_notifies_local_clients(gateway):
    _base_url, state = gateway
    refresh_bootstrap(state, bootstrap_shop(enabled=False))
    first_version = state.event_version

    refresh_bootstrap(state, bootstrap_shop(enabled=True))

    assert state.event_version > first_version
    assert state.shop_ai_takeover == {"mall_100": True}


def test_resume_ai_clears_one_handoff_and_syncs_brain(gateway):
    base_url, state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("handoff-1"))
    state.set_handoff("cs_100:1", "buyer-1", True, "manual intervention")
    forwarded = []
    state.post_remote_json = lambda path, payload: (
        forwarded.append((path, payload)) or {"ok": True}
    )

    response = post_event(base_url, "/api/session/state", {
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "handoff": False,
        "reason": "客服手动恢复 AI 回复",
    })

    assert response == {"ok": True, "local_first": True, "handoff": False}
    assert state.sessions[state._key("cs_100:1", "buyer-1")]["handoff"] is False
    assert get_json(base_url + "/api/sessions?scope=handoff")["sessions"] == []
    assert forwarded == [("/api/session/state", {
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "handoff": False,
        "reason": "客服手动恢复 AI 回复",
    })]


def test_resume_ai_keeps_handoff_when_brain_does_not_confirm(gateway):
    base_url, state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("handoff-failure-1"))
    state.set_handoff("cs_100:1", "buyer-1", True, "manual intervention")

    def fail_remote(_path, _payload):
        raise RuntimeError("offline")

    state.post_remote_json = fail_remote
    req = urlrequest.Request(
        base_url + "/api/session/state",
        data=json.dumps({
            "account": "cs_100:1",
            "buyer_id": "buyer-1",
            "handoff": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urlerror.HTTPError) as caught:
        urlrequest.urlopen(req, timeout=3)

    assert caught.value.code == 502
    assert state.sessions[state._key("cs_100:1", "buyer-1")]["handoff"] is True


def test_both_workbenches_have_unified_ai_takeover_controls() -> None:
    legacy_html = (WEB / "adsorb.html").read_text(encoding="utf-8")
    dock_html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "AI接待中" in legacy_html
    assert "AI已关闭" in legacy_html
    assert "AI影子中" in legacy_html
    assert "handoff=0" in legacy_html
    assert "await req('/api/session/state'" in legacy_html
    assert "ai-state-btn" in dock_html
    assert "await fetchJSON('/api/session/state'" in dock_html
    assert "客服手动开启 AI 接待" in dock_html
    assert "客服手动关闭 AI 接待" in dock_html
    assert "ai_takeover_enabled: enabled" in dock_html
    assert "body.dock #btnMin { display: none; }" in dock_html
    assert "setDockControl({ pin: pinOn })" in dock_html
    assert "setDockControl({ adsorb: adsorbOn })" in dock_html


def test_local_workbench_displays_secondary_realname_fields() -> None:
    dock_html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "raw.secondary_realname" in dock_html
    assert "raw.secondary_realname_action_required" in dock_html
    assert "oc.secondary_realname ?? cardStatus.secondary_realname" in dock_html
    assert "二次实名（接口）" in dock_html
    assert "二次实名处理" in dock_html
    assert "当前使用卡需引导" in dock_html
    assert "当前使用卡无需引导" in dock_html


def test_ai_takeover_toggle_updates_local_state_only_after_brain_confirms(gateway):
    base_url, state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("ai-toggle-1"))
    forwarded = []
    state.post_remote_json = lambda path, payload: (
        forwarded.append((path, payload)) or {
            "ok": True,
            "shop_ai_takeover_enabled": True,
            "ai_takeover_enabled": False,
            "ai_takeover_override": False,
            "ai_takeover_state": "paused",
            "handoff": False,
        }
    )

    response = post_event(base_url, "/api/session/state", {
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "ai_takeover_enabled": False,
        "reason": "客服手动关闭 AI 接待",
    })

    assert response["ai_takeover_state"] == "paused"
    session = state.sessions[state._key("cs_100:1", "buyer-1")]
    assert session["ai_takeover_override"] is False
    assert session["ai_takeover_enabled"] is False
    assert session["handoff"] is False
    listed = get_json(base_url + "/api/sessions?scope=active")["sessions"]
    assert listed[0]["ai_takeover_state"] == "paused"
    assert listed[0]["ai_takeover_enabled"] is False
    assert forwarded == [("/api/session/state", {
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "reason": "客服手动关闭 AI 接待",
        "ai_takeover_enabled": False,
    })]


def test_ai_takeover_toggle_failure_preserves_local_state(gateway):
    base_url, state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("ai-toggle-failure-1"))
    session = state.sessions[state._key("cs_100:1", "buyer-1")]
    session.update({
        "shop_ai_takeover_enabled": True,
        "ai_takeover_enabled": True,
        "ai_takeover_override": None,
        "ai_takeover_state": "active",
    })
    state.post_remote_json = lambda _path, _payload: (_ for _ in ()).throw(RuntimeError("offline"))
    req = urlrequest.Request(
        base_url + "/api/session/state",
        data=json.dumps({
            "account": "cs_100:1",
            "buyer_id": "buyer-1",
            "ai_takeover_enabled": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urlerror.HTTPError) as caught:
        urlrequest.urlopen(req, timeout=3)

    assert caught.value.code == 502
    assert session["ai_takeover_state"] == "active"
    assert session["ai_takeover_override"] is None


def test_canonical_and_legacy_ingress_are_committed_locally(gateway):
    base_url, _state = gateway
    canonical = post_event(
        base_url,
        "/api/bridge/v1/events",
        {"agent_id": "pdd-seat-test", "events": [event("canonical-1")]},
    )
    legacy = post_event(base_url, "/api/local-seat/v1/events", event("legacy-1"))
    assert canonical["event_acks"] == [{
        "event_id": "canonical-1",
        "committed": True,
        "accepted": True,
    }]
    assert legacy["local_ingested"] == 1

    sessions = get_json(base_url + "/api/sessions?scope=active")
    assert sessions["local_first"] is True
    assert sessions["sessions"][0]["msg_count"] == 2


def test_pdd_merchant_setup_tip_is_hidden_from_session_preview(gateway):
    base_url, _state = gateway
    payload = event("merchant-tip-1")
    payload.update({
        "raw_type": 31,
        "content": "您好像还没有配置消费者问到的常见问题回答，为了提升您的接待效率，建议您尽快完善此配置 立即配置",
    })
    post_event(base_url, "/api/bridge/v1/events", {"events": [payload]})
    assert get_json(base_url + "/api/sessions?scope=active")["sessions"] == []


def test_pdd_staff_identity_is_not_exposed_as_buyer_session(gateway):
    base_url, state = gateway
    with state.lock:
        state.shop_accounts = {"mall_100": "cs_100:1"}
        state.shop_platforms = {"mall_100": "pdd", "tb_nick_pdd10015": "pdd"}
        state.observed_shop_ids.add("tb_nick_pdd10015")
        state.sessions[state._key("pdd10015", "1")] = {
            "buyer_id": "1",
            "account": "pdd10015",
            "nickname": "pdd10015",
            "shop_id": "tb_nick_pdd10015",
            "platform": "pdd",
            "messages": [{"msg_id": "self-1", "role": "mall_cs", "content": "reply", "ts": 1}],
            "last_content": "reply",
            "last_role": "mall_cs",
            "last_ts": 1,
        }
    assert get_json(base_url + "/api/sessions?scope=active")["sessions"] == []


def test_first_local_event_is_visible_before_shop_bootstrap(gateway):
    base_url, state = gateway
    with state.lock:
        state.active_shop_ids.clear()
        state.observed_shop_ids.clear()
        state._save_locked()
    response = post_event(
        base_url,
        "/api/bridge/v1/events",
        {"agent_id": "pdd-seat-test", "events": [event("cold-start-1")]},
    )
    assert response["visible"] == 1
    sessions = get_json(base_url + "/api/sessions?scope=active")
    assert sessions["sessions"][0]["last_content"] == "local first message"
    assert sessions["auth"]["shop_ids"] == ["mall_100"]


def test_missing_account_uses_unique_bootstrap_shop(gateway):
    base_url, state = gateway
    with state.lock:
        state.shop_accounts = {"mall_100": "cs_100:88"}
        state.shop_platforms = {"mall_100": "pdd"}
        state._save_locked()
    payload = event("missing-account-1")
    payload["account"] = ""
    response = post_event(base_url, "/api/bridge/v1/events", {"events": [payload]})
    assert response["visible"] == 1
    sessions = get_json(base_url + "/api/sessions?scope=active")["sessions"]
    assert sessions[0]["account"] == "cs_100:88"


def test_agent_enriches_missing_account_from_gateway_state(tmp_path):
    state_path = tmp_path / "data" / "seat_local_state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({
        "active_shop_ids": ["mall_100"],
        "shop_accounts": {"mall_100": "cs_100:88"},
        "shop_platforms": {"mall_100": "pdd"},
    }), encoding="utf-8")

    class Agent:
        cfg = {"config_path": str(tmp_path / "bridge_config.json")}
        platform = type("Platform", (), {"name": "pdd"})()

    enriched = _message_with_local_context(Agent(), {
        "platform": "pdd", "buyer_id": "buyer-1", "content": "hello", "account": "",
    })
    assert enriched["account"] == "cs_100:88"
    assert enriched["shop_id"] == "mall_100"


def test_wrong_machine_token_is_rejected(gateway):
    base_url, _state = gateway
    req = urlrequest.Request(
        base_url + "/api/bridge/v1/events",
        data=json.dumps({"events": [event()]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-Token": "wrong"},
        method="POST",
    )
    with pytest.raises(urlerror.HTTPError) as caught:
        urlrequest.urlopen(req, timeout=3)
    assert caught.value.code == 403


def test_sse_notifies_after_local_commit(gateway):
    base_url, _state = gateway
    received = []

    def listen() -> None:
        with urlrequest.urlopen(base_url + "/api/seat/stream", timeout=3) as response:
            received.append(response.read().decode("utf-8"))

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.1)
    post_event(
        base_url,
        "/api/bridge/v1/events",
        {"agent_id": "pdd-seat-test", "events": [event("stream-1")]},
    )
    thread.join(timeout=3)
    assert received and "event: seat-message" in received[0]


def test_sse_reconnect_resumes_after_last_seen_version(gateway):
    base_url, state = gateway
    post_event(
        base_url,
        "/api/bridge/v1/events",
        {"agent_id": "pdd-seat-test", "events": [event("stream-resume-1")]},
    )
    first_version = state.event_version
    received = []

    def listen() -> None:
        with urlrequest.urlopen(
            base_url + f"/api/seat/stream?after={first_version}", timeout=3
        ) as response:
            received.append(response.read().decode("utf-8"))

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.15)
    assert received == []
    post_event(
        base_url,
        "/api/bridge/v1/events",
        {"agent_id": "pdd-seat-test", "events": [event("stream-resume-2")]},
    )
    thread.join(timeout=3)
    assert received
    assert f"id: {first_version + 1}" in received[0]


def test_workbench_uses_resumable_single_flight_detail_refresh() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "seatLastEventVersion" in html
    assert "/api/seat/stream?after=${encodeURIComponent(seatLastEventVersion)}" in html
    assert "backgroundSessionRefreshRunning" in html
    assert "await refreshCurrentSession(true)" in html
    assert "await fetchJSON('/api/clear_unread'" not in html


def test_agent_local_delivery_queue_retries_and_drains(gateway, tmp_path):
    base_url, _state = gateway
    stop_event = threading.Event()
    queue = LocalDeliveryQueue(
        endpoint=base_url,
        token="local-secret",
        agent_id="pdd-seat-test",
        path=tmp_path / "bridge_local.jsonl",
        stop_event=stop_event,
    )
    try:
        queue.enqueue(event("queued-1"))
        deadline = time.time() + 4
        while time.time() < deadline and queue.status()["pending"]:
            time.sleep(0.05)
        assert queue.status()["pending"] == 0
        assert queue.status()["last_error"] == ""
        sessions = get_json(base_url + "/api/sessions?scope=active")
        assert sessions["sessions"][0]["last_content"] == "local first message"
    finally:
        stop_event.set()
        queue.wakeup.set()
        queue.thread.join(timeout=2)


def test_first_run_gateway_config_requires_real_saved_identity(tmp_path):
    config_path = tmp_path / "bridge_config.json"
    config_path.write_text(json.dumps({
        "agent_token": "center-issued-token",
        "agent_id": "",
        "manage_local_workbench": True,
    }), encoding="utf-8")
    assert pdd_client._gateway_config_ready(config_path) is False

    config_path.write_text(json.dumps({
        "agent_token": "customer-token",
        "agent_id": "pdd-customer-device",
        "manage_local_workbench": True,
    }), encoding="utf-8")
    assert pdd_client._gateway_config_ready(config_path) is True

    config_path.write_text(json.dumps({
        "agent_token": "customer-token",
        "agent_id": "pdd-customer-device",
        "manage_local_workbench": False,
    }), encoding="utf-8")
    assert pdd_client._gateway_config_ready(config_path) is False


def test_first_run_gateway_retries_after_configuration_is_saved(monkeypatch):
    stop_event = threading.Event()
    started = threading.Event()
    checks = iter([False, True])

    monkeypatch.setattr(
        pdd_client,
        "_gateway_config_ready",
        lambda: next(checks, True),
    )
    monkeypatch.setattr(pdd_client, "start_local_gateway", started.set)
    thread = threading.Thread(
        target=pdd_client._retry_local_gateway_after_setup,
        args=(stop_event,),
        daemon=True,
    )
    thread.start()
    assert started.wait(1.5)
    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_jump_helper_cleanup_stops_only_this_bundle(monkeypatch, tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    helper = root / "PddJumpHelper.exe"
    helper.write_bytes(b"test")
    other = tmp_path / "other" / "PddJumpHelper.exe"
    other.parent.mkdir()
    other.write_bytes(b"test")

    class FakeProcess:
        def __init__(self, pid, executable, children=()):
            self.pid = pid
            self._executable = executable
            self._children = list(children)
            self.terminated = False
            self.killed = False

        def exe(self):
            return str(self._executable)

        def children(self, recursive=True):
            assert recursive is True
            return self._children

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    child = FakeProcess(2, helper)
    matching = FakeProcess(1, helper, [child])
    unrelated = FakeProcess(3, other)
    monkeypatch.setattr(
        pdd_client.psutil,
        "process_iter",
        lambda: iter([matching, unrelated]),
    )
    monkeypatch.setattr(
        pdd_client.psutil,
        "wait_procs",
        lambda processes, timeout: (processes, []),
    )

    _stop_jump_helpers(root)
    assert matching.terminated is True
    assert child.terminated is True
    assert unrelated.terminated is False


def test_gateway_cleanup_only_stops_verified_bundle_process(monkeypatch, tmp_path):
    gateway_exe = tmp_path / "LocalSeatGateway.exe"
    config_path = tmp_path / "bridge_config.json"
    gateway_exe.write_bytes(b"test")
    config_path.write_text("{}", encoding="utf-8")

    class FakeProcess:
        terminated = False
        killed = False

        def __init__(self, pid):
            self.pid = pid

        def exe(self):
            return str(gateway_exe)

        def cmdline(self):
            return [
                str(gateway_exe),
                "--ui-role", "seat",
                "--bridge-config", str(config_path),
                "--host", "127.0.0.1",
                "--port", "18766",
            ]

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 5.0
            return 0

        def kill(self):
            self.killed = True

    process = FakeProcess(4321)
    monkeypatch.setattr(pdd_client.psutil, "Process", lambda _pid: process)
    assert _gateway_process_matches(4321, gateway_exe, config_path, 18766) is True
    assert _gateway_process_matches(4321, gateway_exe, config_path, 18767) is False

    pdd_client._GATEWAY_PID = 4321
    pdd_client._GATEWAY_EXE = gateway_exe
    pdd_client._GATEWAY_CONFIG = config_path
    pdd_client._GATEWAY_PORT = 18766
    stop_local_gateway()
    assert process.terminated is True
    assert process.killed is False
    assert pdd_client._GATEWAY_PID is None


def test_pdd_system_template_is_not_parsed_as_customer_message():
    payload = {
        "push_data": {"data": [
            {"message": {
                "from": {"role": "user", "uid": "123456"},
                "to": {"role": "mall_cs", "mall_id": "100", "uid": "100"},
                "content": "机器人已暂停接待，点击立即恢复接待",
                "type": 31,
                "msg_id": "system-1",
                "template_name": "mall_robot_man_intervention_and_restart",
                "no_unreply_hint": 1,
                "conv_silent": True,
            }},
            {"message": {
                "from": {"role": "user", "uid": "123456"},
                "to": {"role": "mall_cs", "mall_id": "100", "uid": "100"},
                "content": "正常咨询",
                "type": 0,
                "msg_id": "customer-1",
            }},
        ]},
    }
    messages = parse_line("buyer_msg=" + json.dumps(payload, ensure_ascii=False), "test")
    assert [message["msg_id"] for message in messages] == ["customer-1"]


def test_gateway_acknowledges_and_discards_pdd_system_event(gateway):
    base_url, state = gateway
    response = post_event(base_url, "/api/bridge/v1/events", {"events": [{
        **event("system-2"),
        "message_type": 31,
        "template_name": "mall_robot_man_intervention_and_restart",
        "no_unreply_hint": True,
        "conv_silent": True,
    }]})
    assert response["event_acks"] == [{
        "event_id": "system-2",
        "committed": True,
        "accepted": False,
        "filtered": True,
        "filter_reason": "pdd_system_message",
    }]
    with state.lock:
        assert all(
            message.get("msg_id") != "system-2"
            for session in state.sessions.values()
            for message in session.get("messages", [])
        )


def test_gateway_purges_cached_pdd_system_message(tmp_path):
    path = tmp_path / "seat_state.json"
    path.write_text(json.dumps({
        "active_shop_ids": ["mall_100"],
        "sessions": {"cs_100:1\u0000123456": {
            "account": "cs_100:1",
            "buyer_id": "123456",
            "shop_id": "mall_100",
            "messages": [
                {"msg_id": "normal-1", "role": "user", "content": "正常咨询", "ts": 1},
                {
                    "msg_id": "system-3",
                    "role": "user",
                    "content": "机器人已暂停接待，点击立即恢复接待",
                    "ts": 2,
                },
            ],
        }},
    }, ensure_ascii=False), encoding="utf-8")
    state = LocalSeatState(path)
    session = next(iter(state.sessions.values()))
    assert [message["msg_id"] for message in session["messages"]] == ["normal-1"]
    assert session["last_content"] == "正常咨询"
    assert session["msg_count"] == 1


def test_remote_only_session_is_not_exposed_on_local_machine(tmp_path):
    state = LocalSeatState(tmp_path / "seat_state.json")
    session_path = "/api/sessions?scope=active"
    detail_path = "/api/session/123456?account=cs_100%3A1"
    system_message = {
        "msg_id": "remote-system-1",
        "role": "user",
        "content": "机器人已暂停接待，点击立即恢复接待",
        "ts": 2,
    }
    with state.lock:
        state.remote_cache[session_path] = {"sessions": [{
            "account": "cs_100:1",
            "buyer_id": "123456",
            "last_content": system_message["content"],
            "last_ts": 2,
        }]}
        state.remote_cache[detail_path] = {
            "ok": True,
            "messages": [
                {"msg_id": "remote-normal-1", "role": "user", "content": "正常咨询", "ts": 1},
                system_message,
            ],
        }
    assert state.merge_sessions(session_path)["sessions"] == []
    assert state.merge_detail(detail_path) is None


def test_other_agent_sessions_stay_out_of_local_views_and_counts(tmp_path):
    state = LocalSeatState(tmp_path / "seat_state.json")
    state.configure(
        backend="http://127.0.0.1:1",
        agent_token="local-secret",
        agent_id="this-seat",
    )
    with state.lock:
        state.active_shop_ids = {"mall_100"}
    state.publish({
        "platform": "pdd",
        "account": "cs_100:1",
        "buyer_id": "buyer-local",
        "msg_id": "local-message",
        "role": "user",
        "content": "received on this machine",
        "ts": 100,
    })
    sessions_path = "/api/sessions?scope=active"
    queue_path = "/api/queue/handoff?limit=80&handoff=0"
    other_detail_path = "/api/session/buyer-other?account=cs_100%3A2"
    other_agent = {
        "account": "cs_100:2",
        "buyer_id": "buyer-other",
        "shop_id": "mall_100",
        "last_content": "received by another agent",
        "last_ts": 200,
    }
    with state.lock:
        state.remote_cache[sessions_path] = {
            "sessions": [other_agent],
            "counts": {"all": 99, "active": 99, "unread": 99},
            "shop_counts": {"mall_100": 99},
            "total": 99,
        }
        state.remote_cache[queue_path] = {"items": [other_agent], "count": 99}
        state.remote_cache[other_detail_path] = {
            "messages": [{
                "msg_id": "other-message",
                "role": "user",
                "content": "received by another agent",
                "ts": 200,
            }],
        }

    sessions = state.merge_sessions(sessions_path)
    assert [(row["account"], row["buyer_id"]) for row in sessions["sessions"]] == [
        ("cs_100:1", "buyer-local"),
    ]
    assert sessions["counts"]["all"] == 1
    assert sessions["shop_counts"] == {"mall_100": 1}
    assert sessions["total"] == 1
    assert state.merge_queue(queue_path)["count"] == 1
    assert state.merge_detail(other_detail_path) is None
    with state.lock:
        state.remote_cache["/api/status"] = {"session_count": 99}
    assert state.merged_status()["session_count"] == 1


def test_same_shop_other_account_stays_out_of_local_views(gateway):
    base_url, state = gateway
    with state.lock:
        state.shop_accounts = {"mall_100": "cs_100:1"}
        state.sessions[state._key("cs_100:2", "buyer-other")] = {
            "buyer_id": "buyer-other",
            "account": "cs_100:2",
            "nickname": "other",
            "shop_id": "mall_100",
            "platform": "pdd",
            "messages": [{"msg_id": "other-1", "role": "user", "content": "other", "ts": 2}],
            "last_content": "other",
            "last_role": "user",
            "last_ts": 2,
        }
        state._save_locked()
    state.publish({**event("mine-1"), "content": "mine"})
    sessions = get_json(base_url + "/api/sessions?scope=active")
    assert [(row["account"], row["buyer_id"]) for row in sessions["sessions"]] == [
        ("cs_100:1", "buyer-1"),
    ]


def test_brain_handoff_enters_local_handoff_scope_even_if_local_state_is_false(gateway):
    base_url, state = gateway
    post_event(base_url, "/api/bridge/v1/events", event("remote-handoff-1"))
    with state.lock:
        session = state.sessions[state._key("cs_100:1", "buyer-1")]
        session["handoff"] = False
        state.remote_cache["/api/sessions?scope=handoff"] = {
            "sessions": [{
                "account": "cs_100:1",
                "buyer_id": "buyer-1",
                "shop_id": "mall_100",
                "last_content": "needs human",
                "last_ts": session["last_ts"] - 1,
                "handoff": True,
                "handoff_reason": "force_handoff",
            }],
        }
    payload = get_json(base_url + "/api/sessions?scope=handoff")
    sessions = payload["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["handoff"] is True
    assert sessions[0]["handoff_reason"] == "force_handoff"
    assert payload["counts"]["handoff_unread"] == 1


def test_brain_handoff_forces_ai_paused_state() -> None:
    """大脑下发“转人工”后，浮窗按钮必须显示“AI已暂停”，而不是继续“AI接待中”。

    浮窗按钮只看 ai_takeover_state；中心下发的 handoff 必须映射成该状态，
    否则按钮会一直显示“AI接待中”。
    """
    row = {
        "shop_id": "mall_1",
        "account": "cs_1:2",
        "ai_takeover_state": "active",
        "ai_takeover_enabled": True,
        "ai_takeover_override": None,
        "handoff": True,
    }
    applied = LocalSeatState._apply_ai_takeover_state(row, {"mall_1": True})
    assert applied["ai_takeover_state"] == "handoff"
    assert applied["ai_takeover_enabled"] is False

    overridden = LocalSeatState._apply_ai_takeover_state(
        {**row, "ai_takeover_override": True}, {"mall_1": True}
    )
    # 转人工优先：旧的“重开 AI” override 不能把按钮顶回 AI接待中（否则一行两个状态）
    assert overridden["ai_takeover_state"] == "handoff"
    assert overridden["ai_takeover_enabled"] is False

    resumed = LocalSeatState._apply_ai_takeover_state(
        {**row, "handoff": False, "ai_takeover_override": True}, {"mall_1": True}
    )
    assert resumed["ai_takeover_state"] == "active"

    shadow = LocalSeatState._apply_ai_takeover_state(
        {**row, "handoff": False}, {"mall_1": False}
    )
    assert shadow["ai_takeover_state"] == "shadow"


def test_brain_handoff_from_session_detail_reaches_local_rows(tmp_path) -> None:
    """中心“会话列表”不含本工位时（归属/作用域问题），转人工标记仍要能从
    会话明细里补到本地行，否则浮窗会一直显示“AI接待中”。"""
    state = LocalSeatState(tmp_path / "seat.json")
    with state.lock:
        state.active_shop_ids = {"mall_1"}
        state.shop_ai_takeover = {"mall_1": True}
        state._save_locked()
    state.publish({
        "platform": "pdd", "event_id": "e1", "msg_id": "m1",
        "account": "cs_1:2", "buyer_id": "buyer-1", "role": "user",
        "content": "在的", "ts": 1789577000,
    })
    # 中心列表为空，但明细可用且带着转人工标记
    state.remote_cache["/api/sessions?paged=1&scope=active&page=1&limit=50"] = {
        "ok": True, "sessions": [], "counts": {},
    }
    state.remote_cache["/api/session/buyer-1?account=cs_1%3A2"] = {
        "ok": True, "handoff": True, "handoff_reason": "traffic_grant_human_handoff",
        "ai_takeover_state": "active", "ai_takeover_enabled": True,
        "messages": [{
            "msg_id": "m1", "role": "user", "content": "在的",
            "ts": 1789577000, "account": "cs_1:2", "buyer_id": "buyer-1",
        }],
    }

    payload = state.merge_sessions("/api/sessions?paged=1&scope=active&page=1&limit=50")
    row = next(item for item in payload["sessions"] if item.get("buyer_id") == "buyer-1")
    assert row["handoff"] is True
    assert row["handoff_reason"] == "traffic_grant_human_handoff"
    assert row["ai_takeover_state"] == "handoff"
    assert row["ai_takeover_enabled"] is False


def test_session_tabs_only_show_recent_and_handoff() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-filter="active"' in html
    assert 'data-filter="handoff"' in html
    assert 'data-filter="unread"' not in html
    assert 'data-filter="history"' not in html
    assert '待人工接入' in html
    assert 'id="handoffUnreadBadge"' in html
    assert 'handoff_unread' in html
    assert 'list = list.filter(s => !!s.handoff);' in html


def test_remote_only_detail_cannot_fall_through_to_center_proxy(gateway):
    base_url, state = gateway
    detail_path = "/api/session/buyer-other?account=cs_100%3A2"
    with state.lock:
        state.remote_cache[detail_path] = {
            "ok": True,
            "messages": [{
                "msg_id": "other-message",
                "role": "user",
                "content": "received by another agent",
                "ts": 200,
            }],
        }

    with pytest.raises(urlerror.HTTPError) as exc_info:
        get_json(base_url + detail_path)
    assert exc_info.value.code == 404
    payload = json.loads(exc_info.value.read().decode("utf-8"))
    assert payload["error"] == "session_not_local"


def test_whitebox_message_detail_is_not_treated_as_a_buyer_session(gateway):
    base_url, _state = gateway
    path = "/api/session/message-detail?sequence=1&buyer_id=buyer-local&account=cs_100%3A1"

    with pytest.raises(urlerror.HTTPError) as exc_info:
        get_json(base_url + path)

    # The test backend is intentionally unreachable. A 502 proves this
    # functional endpoint reached the normal proxy instead of being rejected
    # locally as buyer id `message-detail` with `session_not_local`.
    assert exc_info.value.code == 502
    payload = json.loads(exc_info.value.read().decode("utf-8"))
    assert payload["error"].startswith("backend_unreachable:")


def test_late_history_never_replaces_latest_session_preview(tmp_path):
    path = tmp_path / "seat_state.json"
    state = LocalSeatState(path)
    common = {
        "platform": "pdd",
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "role": "user",
    }
    state.publish({**common, "msg_id": "newer", "content": "latest", "ts": 200})
    state.publish({**common, "msg_id": "older", "content": "history", "ts": 100})
    session = state.sessions[state._key("cs_100:1", "buyer-1")]
    assert [message["msg_id"] for message in session["messages"]] == ["older", "newer"]
    assert session["last_content"] == "latest"
    assert session["last_ts"] == 200

    stored = json.loads(path.read_text(encoding="utf-8"))
    row = next(iter(stored["sessions"].values()))
    row["messages"].reverse()
    row.update({"last_content": "history", "last_role": "user", "last_ts": 100})
    path.write_text(json.dumps(stored), encoding="utf-8")
    repaired = LocalSeatState(path)
    repaired_row = next(iter(repaired.sessions.values()))
    assert [message["msg_id"] for message in repaired_row["messages"]] == ["older", "newer"]
    assert repaired_row["last_content"] == "latest"
    assert repaired_row["last_ts"] == 200


def test_same_second_messages_are_sorted_by_platform_milliseconds(tmp_path):
    state = LocalSeatState(tmp_path / "seat_state.json")
    common = {
        "platform": "pdd",
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "role": "user",
        "ts": 1786182204,
    }
    state.publish({
        **common,
        "msg_id": "newer",
        "content": "second message",
        "platform_ts_key": "1786182204002",
    })
    state.publish({
        **common,
        "msg_id": "older",
        "content": "first message",
        "platform_ts_key": "1786182204001",
    })
    session = state.sessions[state._key("cs_100:1", "buyer-1")]
    assert [message["msg_id"] for message in session["messages"]] == ["older", "newer"]


def test_remote_ai_reply_wins_preview_and_is_sorted_after_local_buyer(tmp_path):
    state = LocalSeatState(tmp_path / "seat_state.json")
    state.publish({
        "platform": "pdd",
        "account": "cs_100:1",
        "buyer_id": "buyer-1",
        "msg_id": "buyer-message",
        "role": "user",
        "content": "question",
        "ts": 100,
    })
    sessions_path = "/api/sessions?scope=active"
    detail_path = "/api/session/buyer-1?account=cs_100%3A1"
    ai_message = {
        "msg_id": "shadow-reply",
        "role": "assistant_simulated",
        "content": "answer",
        "ts": 101,
    }
    with state.lock:
        state.remote_cache[sessions_path] = {"ok": True, "sessions": [{
            "account": "cs_100:1",
            "buyer_id": "buyer-1",
            "shop_id": "mall_100",
            "last_content": "stale preview",
            "last_role": "mall_cs",
            "last_ts": 100,
        }]}
        state.remote_cache[detail_path] = {"ok": True, "messages": [ai_message]}
    merged_sessions = state.merge_sessions(sessions_path)
    assert merged_sessions["sessions"][0]["last_content"] == "answer"
    assert merged_sessions["sessions"][0]["display_last_content"] == "answer"
    assert merged_sessions["sessions"][0]["last_role"] == "assistant_simulated"
    merged_detail = state.merge_detail(detail_path)
    assert [message["msg_id"] for message in merged_detail["messages"]] == [
        "buyer-message",
        "shadow-reply",
    ]


def test_center_upload_waits_for_local_commit(tmp_path):
    pdd_client.install_local_first()
    event_id = "local-before-center-1"
    local_commit_at = []
    center_upload_at = []

    class LocalDelivery:
        def __init__(self):
            self.pending = {event_id}
            self.lock = threading.Lock()

        def pending_ids(self):
            with self.lock:
                return set(self.pending)

        def commit(self):
            with self.lock:
                self.pending.clear()
                local_commit_at.append(time.monotonic())

    class Client:
        def upload_events(self, _batch):
            center_upload_at.append(time.monotonic())
            return {"ok": True}

    agent = object.__new__(BridgeAgent)
    agent._stop = threading.Event()
    agent._pending = deque([{"event_id": event_id, "ts": time.time()}])
    agent._pending_lock = threading.Lock()
    agent._immediate_upload_started = True
    agent._immediate_upload_lock = threading.Lock()
    agent._local_first_gate_lock = threading.RLock()
    agent._local_first_deadlines = {event_id: time.monotonic() + 1.0}
    agent._local_delivery = LocalDelivery()
    agent.client = Client()
    agent.queue_path = tmp_path / "center.jsonl"
    agent._last_error = ""
    timer = threading.Timer(0.08, agent._local_delivery.commit)
    timer.start()
    try:
        agent._flush_events()
    finally:
        timer.join(timeout=1)
    assert local_commit_at
    assert center_upload_at
    assert center_upload_at[0] >= local_commit_at[0]
