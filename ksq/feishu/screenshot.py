"""Render a compact robot-log screenshot around an abnormal event."""

from __future__ import annotations

import io
import re
import textwrap
from typing import List, Sequence

from ksq.feishu.log_parser import clean_log_lines

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - runtime image normally includes Pillow
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _font():
    if ImageFont is None:
        raise RuntimeError("Pillow unavailable")
    for path in _FONTS:
        try:
            return ImageFont.truetype(path, 14)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_color(line: str) -> str:
    if re.search(r"\[ERROR\]|\bERROR\b|失败|错误", line, re.I):
        return "#ff9b9b"
    if re.search(r"\[WARNING\]|\bWARN(?:ING)?\b|警告", line, re.I):
        return "#f5cf79"
    if re.search(r"\[DEBUG\]|\bDEBUG\b", line, re.I):
        return "#86b7ff"
    return "#d6deea"


def _window(raw_logs: object, message: object, before: int, after: int) -> tuple[List[str], int]:
    lines = [str(row.get("raw") or "") for row in clean_log_lines(raw_logs)]
    if not lines:
        return [], -1
    needle = str(message or "").strip()[:100]
    anchor = len(lines) - 1
    if needle:
        for index in range(len(lines) - 1, -1, -1):
            if needle in lines[index]:
                anchor = index
                break
    start = max(0, anchor - before)
    end = min(len(lines), anchor + after + 1)
    return lines[start:end], anchor - start


def render_log_screenshot(
    raw_logs: object,
    message: object,
    before: int = 20,
    after: int = 20,
) -> bytes:
    lines, anchor = _window(raw_logs, message, before, after)
    if not lines or Image is None or ImageDraw is None:
        return b""
    font = _font()
    probe = Image.new("RGB", (10, 10), "black")
    draw = ImageDraw.Draw(probe)
    # Keep the original content but wrap it for a readable terminal-style
    # image instead of truncating the diagnostic tail with an ellipsis.
    display_lines: List[str] = []
    display_anchor = 0
    for index, line in enumerate(lines):
        wrapped = textwrap.wrap(
            line,
            width=128,
            replace_whitespace=False,
            drop_whitespace=False,
        ) or [""]
        if index == anchor:
            display_anchor = len(display_lines)
        display_lines.extend(wrapped)
    boxes = [draw.textbbox((0, 0), line, font=font) for line in display_lines]
    width = max([1180] + [min(box[2] - box[0] + 48, 1500) for box in boxes])
    line_height = max([18] + [box[3] - box[1] + 6 for box in boxes])
    title = "robot_workspace_move_test  ·  原始日志"
    image = Image.new("RGB", (width, 72 + line_height * len(display_lines)), "#0b1220")
    canvas = ImageDraw.Draw(image)
    canvas.rectangle((0, 0, width, 32), fill="#172033")
    for x, color in ((16, "#ff5f57"), (38, "#febc2e"), (60, "#28c840")):
        canvas.ellipse((x, 10, x + 10, 20), fill=color)
    canvas.text((88, 8), title, fill="#b8c5d6", font=font)
    y = 44
    for index, line in enumerate(display_lines):
        if index == display_anchor:
            canvas.rectangle((8, y - 2, width - 8, y + line_height - 1), fill="#3b1d1d")
        canvas.text((16, y), line, fill="#ffb4b4" if index == display_anchor else _line_color(line), font=font)
        y += line_height
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":  # pragma: no cover
    assert _window("a\nerror here\nb", "error here", 1, 1) == (
        ["a", "error here", "b"],
        1,
    )
    print("screenshot self-check ok")
