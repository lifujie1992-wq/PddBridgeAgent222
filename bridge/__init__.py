# -*- coding: utf-8 -*-
"""PddBridgeAgent — local Tanyu channel bridge for multi-seat deployments.

Does NOT run AI/brain. Only:
  - watch local Tanyu logs
  - report messages to center
  - receive send commands and call local DLL
"""

import hashlib

__version__ = "0.6.2.3"
__build_hash__ = hashlib.sha256(
    f"PddBridgeAgent|{__version__}|pdd-imws-v2-business-message".encode("utf-8")
).hexdigest()[:16]
