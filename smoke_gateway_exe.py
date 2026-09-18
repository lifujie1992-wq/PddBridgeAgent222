from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parent
EXE = ROOT / "build-dist-gateway-final" / "LocalSeatGateway" / "LocalSeatGateway.exe"
CONFIG = ROOT / "smoke_config.json"
BASE_URL = "http://127.0.0.1:18770"


def open_json(url: str, *, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="GET" if data is None else "POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Token": "smoke-secret",
            "X-Agent-Id": "smoke-agent",
        },
    )
    with request.urlopen(req, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    state_path = EXE.parent / "data" / "seat_local_state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps({
        "active_shop_ids": ["mall_100"],
        "observed_shop_ids": [],
        "shop_accounts": {"mall_100": "cs_100:88"},
        "shop_platforms": {"mall_100": "pdd"},
        "sessions": {},
    }), encoding="utf-8")
    process = subprocess.Popen(
        [
            str(EXE),
            "--ui-role", "seat",
            "--bridge-config", str(CONFIG),
            "--host", "127.0.0.1",
            "--port", "18770",
        ],
        cwd=str(EXE.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                print("status", open_json(BASE_URL + "/api/seat/status"))
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("gateway did not become healthy")

        payload = {
            "agent_id": "smoke-agent",
            "events": [{
                "event_id": "smoke-exe-001",
                "idempotency_key": "smoke-exe-001",
                "platform": "pdd",
                "account": "",
                "shop_id": "",
                "buyer_id": "buyer-001",
                "msg_id": "smoke-exe-001",
                "role": "user",
                "content": "local-first-smoke",
                "ts": time.time(),
            }],
        }
        try:
            print("post", open_json(BASE_URL + "/api/bridge/v1/events", payload=payload))
            sessions = open_json(BASE_URL + "/api/sessions?scope=active")
            print("sessions", sessions)
            if sessions["sessions"][0]["account"] != "cs_100:88":
                raise RuntimeError("compiled gateway did not enrich the missing account")
            system_payload = {"events": [{
                **payload["events"][0],
                "event_id": "smoke-system-001",
                "msg_id": "smoke-system-001",
                "content": "机器人已暂停接待，点击立即恢复接待",
                "message_type": 31,
                "template_name": "mall_robot_man_intervention_and_restart",
                "no_unreply_hint": True,
                "conv_silent": True,
            }]}
            filtered = open_json(BASE_URL + "/api/bridge/v1/events", payload=system_payload)
            print("filtered", filtered)
            if filtered["event_acks"][0].get("filter_reason") != "pdd_system_message":
                raise RuntimeError("compiled gateway did not filter the PDD system event")
            after = open_json(BASE_URL + "/api/sessions?scope=active")
            if after["sessions"][0]["msg_count"] != 1:
                raise RuntimeError("filtered event leaked into the compiled gateway store")
        except error.HTTPError as exc:
            print("http-error", exc.code, exc.read().decode("utf-8", "replace"))
            raise
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        print("stdout:\n" + stdout)
        print("stderr:\n" + stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
