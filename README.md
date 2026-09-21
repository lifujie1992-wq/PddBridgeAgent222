# PddBridgeAgent 0.5.15 source

This directory is based on the code-object-verified 0.5.13 customer build.
It contains only the PDD Windows bridge, local seat gateway, web assets, and
client tests. The backend repository is not part of this source tree.

Build with Python 3.10:

```powershell
.\build_release.ps1
```

Run tests:

```powershell
$env:PYTHONPATH=$PWD
python -m pytest -q test_local_first_client.py test_gui_config.py test_v0514.py tests/test_pdd_business_message_v0515.py
```

PDD uses `127.0.0.1:18767` by default. Port `18766` is left available for the
Qianniu local gateway when both clients run on the same Windows machine.

The local seat sorts messages by platform time, refreshes when remote AI
details change, and gates normal center uploads until the loopback queue has
committed the same event. If the loopback service is unavailable, the center
upload is released after one second so the two delivery paths remain isolated.

For PDD buyer messages, the agent reads the matching mall's right-side order
panel through its existing local CDP endpoint. The center event remains in the
durable queue until the lookup finishes. Successful empty results and lookup
failures are represented separately; multiple recent orders are never guessed.

PDD `user_source` product details are read from `info.goods_info` and linked to
the following buyer text through the platform `pre_msg_id`. The link is scoped
to the exact shop account and buyer so product context cannot leak between chats.

PDD `business_message` seller callbacks use layered JSON decoding and a bounded
256 KiB/two-second per-source cross-line buffer. The built-in parser profile is
`pdd-imws-v2-business-message`.

The customer build includes `PddAdsorbWindow.exe`. Once the local PDD workbench
is healthy, `PddBridgeAgent.exe` starts this dock automatically by default. Set
`auto_start_with_bridge` to `false` in `pdd_adsorb_config.json` to keep manual-only
startup.
