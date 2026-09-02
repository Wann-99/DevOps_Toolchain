"""Analyze compact abnormal events with an OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Dict, Mapping, Sequence


ERROR_CATEGORIES = (
    "无异常",
    "视觉识别异常",
    "抓取异常",
    "扫码异常",
    "放置异常",
    "导航异常",
    "通信异常",
    "软件逻辑异常",
    "硬件异常",
    "环境异常",
    "未知",
)
ERROR_SUBCATEGORIES = (
    "OCR识别失败",
    "SKU识别失败",
    "吸盘漏气",
    "抓取失败",
    "真空检测失败",
    "扫码超时",
    "扫码失败",
    "二维码损坏",
    "导航超时",
    "定位失败",
    "放置失败",
    "机械臂碰撞",
    "网络超时",
    "通信超时",
    "软件异常",
    "无异常",
    "其它",
)

SYSTEM_PROMPT = """你是一名机器人自动化测试分析助手。

你的任务是根据机器人测试结果生成飞书测试表单需要填写的内容。

要求：
1. 只能依据输入内容分析
2. 不允许猜测不存在的信息
3. 若异常经过重试恢复且最终任务成功，应描述为“重试后恢复”
4. 输出必须为 JSON
5. 不输出 Markdown
6. 不输出解释
7. problem_description 不超过50字
8. error_category、error_subcategory 必须使用固定枚举"""
_DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_DEFAULT_MODEL = "gpt-4o-mini"


def should_analyze(task_status: object, events: object) -> bool:
    return bool(events) or str(task_status or "").strip().upper() != "SUCCESS"


def build_ai_prompt(
    task_status: object,
    skus: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
) -> str:
    payload: Dict[str, object] = {
        "task_status": str(task_status or "UNKNOWN").strip().upper(),
    }
    for index, sku in enumerate(skus, start=1):
        if sku.get("executed") is False:
            payload["sku%d" % index] = {"executed": False}
        else:
            payload["sku%d" % index] = {
                stage: sku.get(stage)
                for stage in ("recognize", "pick", "scan", "place")
            }
    payload["abnormal_events"] = [
        {
            key: event.get(key)
            for key in ("type", "message", "retry", "recovered")
        }
        for event in events[:20]
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _short(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:50]


def _empty(error: str = "") -> Dict[str, object]:
    result: Dict[str, object] = {
        "problem_description": "",
        "error_category": "",
        "error_subcategory": "",
        "need_manual_check": True,
        "used_ai": False,
    }
    if error:
        result["ai_error"] = error
    return result


def _validate(payload: object, recovered: bool) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        return _empty("AI 输出不是 JSON 对象")
    description = _short(payload.get("problem_description"))
    if recovered and "重试后恢复" not in description:
        description = _short("重试后恢复：" + description)
    category = str(payload.get("error_category") or "").strip()
    subcategory = str(payload.get("error_subcategory") or "").strip()
    errors = []
    if not description:
        errors.append("problem_description 为空")
    if category not in ERROR_CATEGORIES:
        errors.append("error_category 不在固定枚举")
        category = ""
    if subcategory not in ERROR_SUBCATEGORIES:
        errors.append("error_subcategory 不在固定枚举")
        subcategory = ""
    manual = payload.get("need_manual_check")
    if not isinstance(manual, bool):
        errors.append("need_manual_check 不是布尔值")
        manual = True
    result: Dict[str, object] = {
        "problem_description": description,
        "error_category": category,
        "error_subcategory": subcategory,
        "need_manual_check": manual or bool(errors),
        "used_ai": True,
    }
    if errors:
        result["ai_error"] = "；".join(errors)
    return result


def _request_analysis(prompt: str, config: Mapping[str, object]) -> object:
    endpoint = str(config.get("endpoint") or _DEFAULT_ENDPOINT).strip()
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("model") or _DEFAULT_MODEL).strip()
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": max(64, min(1000, int(config.get("max_tokens") or 180))),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": prompt
                    + "\n固定枚举："
                    + json.dumps(
                        {
                            "error_category": ERROR_CATEGORIES,
                            "error_subcategory": ERROR_SUBCATEGORIES,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    content = response_payload["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping)
        )
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip(), flags=re.I
    )
    return json.loads(text)


def analyze_events(
    events: Sequence[Mapping[str, object]],
    task_status: object,
    skus: Sequence[Mapping[str, object]] = (),
    config: Mapping[str, object] | None = None,
) -> Dict[str, object]:
    """Call AI only for abnormal/failed tests and only with structured data."""
    if not should_analyze(task_status, events):
        return {
            "problem_description": "无异常",
            "error_category": "无异常",
            "error_subcategory": "无异常",
            "need_manual_check": False,
            "used_ai": False,
            "skipped": True,
        }
    ai = config if isinstance(config, Mapping) else {}
    if not bool(ai.get("enabled")):
        return _empty("异常需要 AI 分析，但 AI 未启用")
    if not str(ai.get("api_key") or "").strip():
        return _empty("异常需要 AI 分析，但 API Key 未配置")
    try:
        payload = _request_analysis(build_ai_prompt(task_status, skus, events), ai)
        return _validate(payload, any(bool(event.get("recovered")) for event in events))
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        return _empty(str(error))
