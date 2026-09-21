"""Immutable capture times and the center's 90-second live-message window."""
import math
import os
import time
from datetime import datetime

LIVE_MAX_AGE_SECONDS = 90.0
# Platform timestamps are second-granular and the platform clock routinely runs a few
# hundred ms ahead of the local host. Without a tolerance every such frame is stamped
# ``future_platform_time`` and then hard-blocked by ``history_ready`` below, so real-time
# messages pile up in the local queue and never reach the center ("some upload, some don't").
FUTURE_TOLERANCE_SECONDS = float(os.environ.get("PDD_FUTURE_TOLERANCE_SECONDS") or 5.0)
# Auto-takeover replies point back at the buyer message they answer
# (``meta.takeover_parent_msg_id``). The center can lease a command tens of seconds after
# creating it, so the original 90 s parent window dropped valid replies with
# ``automatic_command_expired_or_unverifiable_time``. The command's own ``created_at`` keeps
# the strict 90 s window; the parent message gets a wider, configurable budget instead.
TAKEOVER_PARENT_MAX_AGE_SECONDS = float(
    os.environ.get("PDD_TAKEOVER_PARENT_MAX_AGE_SECONDS") or 300.0)
TIMING_FIELDS = ("captured_at", "captured_at_ms", "enqueued_at", "is_history",
                 "auto_reply_eligible", "history_reason", "capture_source", "log_file")


def epoch_seconds(value):
    try:
        number = float(value)
        if number > 100_000_000_000:
            number /= 1000.0
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return date.timestamp() if date.tzinfo else 0.0
        except (ValueError, TypeError, OverflowError):
            return 0.0


def stamp_message(message, *, historical=False, now=None):
    row = dict(message)
    now = time.time() if now is None else now
    captured = epoch_seconds(row.get("captured_at")) or epoch_seconds(row.get("captured_at_ms")) or now
    row["captured_at"] = captured
    row["captured_at_ms"] = int(round(captured * 1000))
    timestamp = epoch_seconds(row.get("ts"))
    source = str(row.get("source") or "")
    history = historical or row.get("is_history") is True or "history" in source
    reason = str(row.get("history_reason") or "")
    if not timestamp:
        history, reason = True, "missing_platform_time"
    elif timestamp > now + FUTURE_TOLERANCE_SECONDS:
        history, reason = True, "future_platform_time"
    else:
        # The platform timestamp caught up with the local clock: a stale "future" verdict
        # (from an older queue row, or from sub-second clock skew) must not stick forever.
        if reason == "future_platform_time":
            reason = ""
        if now - timestamp > LIVE_MAX_AGE_SECONDS:
            history, reason = True, reason or "stale_platform_time"
    if history:
        row["is_history"] = True
        row["auto_reply_eligible"] = False
        row["history_reason"] = reason or "replay"
        row["capture_source"] = row.get("capture_source") or source
        row["source"] = "history"
    return row


def history_ready(event, now):
    """Older centers ignore history flags: never submit a replay inside their live window."""
    timestamp = epoch_seconds(event.get("ts"))
    if not timestamp or event.get("history_reason") == "future_platform_time":
        return False
    return not event.get("is_history") or now - timestamp > LIVE_MAX_AGE_SECONDS


def command_expired(command, now):
    meta = command.get("meta") if isinstance(command.get("meta"), dict) else {}
    kind = str(command.get("type") or "send_text").strip() or "send_text"
    if kind == "open_chat" or meta.get("manual_direct") is True:
        return False
    created = epoch_seconds(command.get("created_at"))
    return not created or created > now or now - created > LIVE_MAX_AGE_SECONDS


def queue_metrics(events, now=None):
    now = time.time() if now is None else now
    rows = list(events)
    captures = [epoch_seconds(e.get("captured_at")) for e in rows]
    captures = [value for value in captures if value]
    delays = [max(0.0, epoch_seconds(e.get("captured_at")) - epoch_seconds(e.get("ts")))
              for e in rows if epoch_seconds(e.get("ts")) and epoch_seconds(e.get("captured_at"))]
    return {"pending": len(rows),
            "oldest_wait_seconds": round(max(0.0, now - min(captures)), 3) if captures else 0.0,
            "max_capture_delay_seconds": round(max(delays, default=0.0), 3),
            "history_pending": sum(bool(e.get("is_history")) for e in rows)}
