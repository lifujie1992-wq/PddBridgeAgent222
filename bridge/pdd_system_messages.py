# -*- coding: utf-8 -*-
"""Identify PDD operator notices without treating buyer text as notices."""

def is_pdd_system_event(value: dict) -> bool:
    if not isinstance(value, dict):
        return False
    event = value.get("event") if isinstance(value.get("event"), dict) else value
    template = str(event.get("template_name") or event.get("template") or "").strip().lower()
    if template == "mall_robot_man_intervention_and_restart":
        return True
    try:
        message_type = int(event.get("raw_type", event.get("message_type", event.get("type", -1))))
    except (TypeError, ValueError):
        message_type = -1
    content = " ".join(str(event.get("content") or "").split())
    if message_type == 31 and (
        ("机器人未找到对应的回复" in content and "点击添加" in content)
        or
        (bool(event.get("no_unreply_hint")) and bool(event.get("conv_silent")))
        or ("还没有配置消费者问到的常见问题回答" in content and "立即配置" in content)
    ):
        return True
    return "机器人已暂停接待" in content and "立即恢复接待" in content

