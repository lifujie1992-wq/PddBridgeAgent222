# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from .pdd import PddPlatform
from .taobao import TaobaoPlatform

_REGISTRY = {
    "pdd": PddPlatform,
    "cnpdd": PddPlatform,
    "taobao": TaobaoPlatform,
    "tb": TaobaoPlatform,
    "qn": TaobaoPlatform,
    "qianniu": TaobaoPlatform,
    "cntaobao": TaobaoPlatform,
}


def normalize_platform(value: Any) -> str:
    key = str(value or "pdd").strip().lower()
    if key in _REGISTRY:
        cls = _REGISTRY[key]
        return cls.name
    return "pdd"


def get_platform(value: Any = "pdd"):
    name = normalize_platform(value)
    for key, cls in _REGISTRY.items():
        if cls.name == name:
            return cls()
    return PddPlatform()
