PddBridgeAgent 0.5.15

Start: double-click PddBridgeAgent.exe
Local workbench: http://127.0.0.1:18767/

This complete PDD package fixes mall_cs business_message capture with layered
JSON decoding and bounded cross-line buffering. It does not include or modify
the backend or Qianniu client.

First installation:
1. Extract the complete PddBridgeAgent-v0.5.15 directory.
2. Copy in the machine's existing bridge_config.json (not included in the ZIP).
3. Start PddBridgeAgent.exe.
4. The PDD docked handoff window starts automatically after the local workbench
   is healthy. Microsoft Edge is required for this narrow dock window.
5. Confirm heartbeat version=0.5.15, parser_profile version=
   pdd-imws-v2-business-message, and a non-empty build_hash.

Upgrade:
- Close PddBridgeAgent first.
- Replace PddBridgeAgent.exe, _internal/, LocalSeatGateway.exe,
  _gateway_internal/, PddAdsorbWindow.exe and pdd_adsorb_config.json from this
  package.
- Keep bridge_config.json, data/, bridge_queue_pdd.jsonl,
  bridge_queue_pdd_local.jsonl, bridge_commands_pdd.json and local logs.
- Do not copy a stale parser_profile_pdd.last_good.json unless it is a center-
  issued compatible profile.
- This ZIP intentionally contains no machine-specific config, token, device
  identity, queue, log or local conversation state.

Docked handoff window:
- Starting PddBridgeAgent.exe starts the dock by default after the PDD local
  workbench is ready. A second dock is not created when one is already running.
- Closing the bridge closes the dock instance that bridge started. It does not
  close PDD Workbench or another independently started dock.
- Set auto_start_with_bridge=false in pdd_adsorb_config.json to opt out.
- The included start/stop CMD files remain available for manual control.

Port allocation:
- Shared center service: 18765
- Qianniu local workbench: 18766
- PDD local workbench: 18767

Safety:
- The package does not automatically replay historical logs.
- Perform any historical mall_cs backfill only with an explicit offline export,
  human review and center-side idempotency controls.
