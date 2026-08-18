"""Collect robot log context and problem description for failed Feishu rows."""

from __future__ import annotations

import io
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ksq.feishu.form_payload import OUTCOME_FAILED, shelf_code_from_location

try:
    from PIL import Image, ImageDraw, ImageFont

    _HAS_PIL = True
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    _HAS_PIL = False

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]"
)

ERROR_ANCHORS = (
    "报错，请求人工处理",
    "放置流程失败，请人工协助",
    "工单已被取消",
    "工单已被人工抢占",
    "数据录入问题",
    "packing task failed",
    "pick_up_object failed",
    "object is marked as unavailable",
    "find object and shelf failed",
    "await_error",
    "[ERROR]",
    " ERROR ",
)

CONTEXT_LINES = 20
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    # 运行镜像 v1.1.1 起内置文泉驿正黑（slim 基础镜像原本没有任何字体）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _load_log_font():
    if not _HAS_PIL:
        raise RuntimeError("PIL unavailable")
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, 14)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _split_log_lines(raw_logs: str) -> List[str]:
    lines: List[str] = []
    for line in str(raw_logs or "").splitlines():
        text = _strip_ansi(line).rstrip()
        if text:
            lines.append(text)
    return lines


def find_error_anchor_index(lines: Sequence[str], hint: object) -> int:
    hint_text = _strip_ansi(str(hint or "")).strip()
    if hint_text:
        for index in range(len(lines) - 1, -1, -1):
            if hint_text[:80] and hint_text[:80] in lines[index]:
                return index
    for index in range(len(lines) - 1, -1, -1):
        body = lines[index]
        if any(anchor in body for anchor in ERROR_ANCHORS):
            return index
    if lines:
        return len(lines) - 1
    return -1


def extract_log_window(
    raw_logs: str, hint: object, context_lines: int, after_lines: Optional[int] = None
) -> Tuple[List[str], int, str]:
    """Lines around the error anchor. after_lines 不给就前后对称，内置工单表照旧。"""
    after = context_lines if after_lines is None else after_lines
    lines = _split_log_lines(raw_logs)
    if not lines:
        return [], -1, ""
    anchor = find_error_anchor_index(lines, hint)
    if anchor < 0:
        return [], -1, ""
    start = max(0, anchor - context_lines)
    end = min(len(lines), anchor + after + 1)
    window = list(lines[start:end])
    return window, anchor - start, lines[anchor]


def window_caption(before: int, after: Optional[int] = None) -> str:
    """「前后各 20 行」/「前 400 后 200 行」—— 对称时的文案必须逐字不变。"""
    tail = before if after is None else after
    return "前后各 %d 行" % before if tail == before else "前 %d 后 %d 行" % (before, tail)


def build_problem_description(
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    outcome: str,
    error_line: str,
    await_kind: object,
    before: int = CONTEXT_LINES,
    after: Optional[int] = None,
) -> str:
    order_no = str(order.get("order_no") or "").strip() or "—"
    task_id = str(order.get("task_id") or "").strip() or "—"
    kind = str(await_kind or "").strip() or "—"
    failed_items: List[str] = []
    for task in tasks:
        status = str(task.get("status") or "").strip().lower()
        if status not in {"failed", "await_error"}:
            continue
        code = str(task.get("barcode") or task.get("code") or "").strip()
        name = str(task.get("name") or "").strip()
        location = str(task.get("location_code") or "").strip()
        shelf = shelf_code_from_location(location)
        label = code or "未知货号"
        if name:
            label = "%s（%s）" % (label, name)
        if location:
            label = "%s 库位=%s 货架=%s" % (label, location, shelf)
        failed_items.append(label)

    headline = "工单执行失败，机器人请求人工处理或任务执行报错。"

    parts = [
        headline,
        "工单号：%s" % order_no,
        "task_id：%s" % task_id,
        "门禁类型：%s" % kind,
        "达成情况：%s" % outcome,
    ]
    if failed_items:
        parts.append("失败SKU：%s" % "；".join(failed_items))
    else:
        parts.append("失败SKU：未定位到具体SKU（见日志附件）")
    if error_line:
        parts.append("报错行：%s" % error_line[:300])
    parts.append(
        "已从 robot_workspace_move_test 截取报错行%s日志，见「问题截图/视频」附件。"
        % window_caption(before, after)
    )
    return "\n".join(parts)


def _measure_text(draw, text, font) -> Tuple[int, int]:
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    width, height = draw.textsize(text, font=font)
    return int(width), int(height)


def render_log_screenshot_png(
    window_lines: Sequence[str],
    anchor_offset: int,
    before: int = CONTEXT_LINES,
    after: Optional[int] = None,
) -> bytes:
    if not _HAS_PIL:
        return b""
    font = _load_log_font()
    probe = Image.new("RGB", (10, 10), "black")
    probe_draw = ImageDraw.Draw(probe)
    line_sizes = []
    max_width = 0
    line_height = 0
    for line in window_lines:
        display = line if len(line) <= 180 else line[:177] + "..."
        width, height = _measure_text(probe_draw, display, font)
        line_sizes.append((display, width, height))
        if width > max_width:
            max_width = width
        if height > line_height:
            line_height = height
    if line_height <= 0:
        line_height = 16
    padding = 16
    gap = 6
    # 标题里历来没空格，去掉 window_caption 的空格保持内置工单表的截图逐字不变。
    title = "robot_workspace_move_test · 报错上下文（%s）" % window_caption(
        before, after
    ).replace(" ", "")
    title_w, title_h = _measure_text(probe_draw, title, font)
    img_w = max(900, max(max_width, title_w) + padding * 2)
    img_h = padding * 2 + title_h + 12 + len(line_sizes) * (line_height + gap)
    image = Image.new("RGB", (img_w, img_h), "#0b1220")
    draw = ImageDraw.Draw(image)
    draw.text((padding, 8), title, fill="#8aa0b8", font=font)
    y = padding + title_h + 10
    for index, (display, _width, height) in enumerate(line_sizes):
        if index == anchor_offset:
            draw.rectangle(
                (8, y - 2, img_w - 8, y + height + 4),
                fill="#3b1d1d",
            )
            color = "#ffb4b4"
        else:
            color = "#d7e0ea"
        draw.text((padding, y), display, fill=color, font=font)
        y += height + gap
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def collect_failure_evidence(
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    outcome: str,
    raw_logs: str,
    await_kind: object,
    await_line: object,
    before: int = CONTEXT_LINES,
    after: Optional[int] = None,
) -> Optional[Dict[str, object]]:
    if outcome != OUTCOME_FAILED:
        return None
    hint = await_line
    if not hint:
        for task in tasks:
            status = str(task.get("status") or "").strip().lower()
            if status in {"failed", "await_error"}:
                hint = task.get("await_line") or task.get("end_line") or ""
                if hint:
                    break
    window, anchor_offset, error_line = extract_log_window(
        raw_logs, hint, before, after
    )
    description = build_problem_description(
        order, tasks, outcome, error_line, await_kind, before, after
    )
    png_bytes = b""
    txt_body = ""
    if window:
        txt_body = "\n".join(window) + "\n"
        try:
            png_bytes = render_log_screenshot_png(window, anchor_offset, before, after)
        except (OSError, RuntimeError, UnicodeEncodeError, ValueError, TypeError):
            png_bytes = b""
    else:
        description = description + "\n（未能从日志中截取到报错上下文）"
    order_no = str(order.get("order_no") or "order").strip() or "order"
    return {
        "description": description,
        "error_line": error_line,
        "window_lines": window,
        "png_bytes": png_bytes,
        "txt_body": txt_body,
        "png_name": "robot_error_%s.png" % order_no,
        "txt_name": "robot_error_%s.txt" % order_no,
    }


def _demo() -> None:
    """默认对称、显式不对称；内置工单表走默认分支，产出必须一字不变。"""
    logs = "\n".join("line%d" % index for index in range(1000))
    logs = logs.replace("line500", "line500 报错，请求人工处理")

    window, offset, error_line = extract_log_window(logs, "", 20)
    assert len(window) == 41 and offset == 20, (len(window), offset)
    assert "报错，请求人工处理" in error_line
    assert window_caption(20) == "前后各 20 行"

    window, offset, _ = extract_log_window(logs, "", 400, 200)
    assert len(window) == 601 and offset == 400, (len(window), offset)
    assert window_caption(400, 200) == "前 400 后 200 行"

    # 前面行数不够时窗口自然截断，锚点偏移跟着缩，不能越界。
    window, offset, _ = extract_log_window("报错，请求人工处理\na\nb", "", 400, 200)
    assert (len(window), offset) == (3, 0), (len(window), offset)

    text = build_problem_description({}, [], "失败", "boom", "error")
    assert "截取报错行前后各 20 行日志" in text, text
    text = build_problem_description({}, [], "失败", "boom", "error", 400, 200)
    assert "截取报错行前 400 后 200 行日志" in text, text
    print("failure_evidence self-check ok")


if __name__ == "__main__":
    _demo()
