"""Display formatting for UI payloads."""

from __future__ import annotations

from ksq.constants import EMPTY_DISPLAY_PLACEHOLDERS


def display_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            item_text = display_value(item)
            if item_text != "-":
                parts.append(item_text)
        return "、".join(parts) if parts else "-"
    text = str(value).strip()
    if text in EMPTY_DISPLAY_PLACEHOLDERS:
        return "-"
    return text
