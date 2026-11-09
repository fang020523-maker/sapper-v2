"""Extract the first valid JSON object from model output.

Lifted from sapper.py so the competition shell does not import the
graph module (which pulls fcntl) just to read a model reply. The
tolerance for prose preambles with stray braces is deliberate: hostile
or chatty targets regularly end up quoted in a model's answer.
"""
from __future__ import annotations

import json


class ModelParseError(Exception):
    pass


def _extract_first_balanced(text: str, start: int) -> tuple[dict | None, int]:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = text[start:i + 1]
                    try:
                        return json.loads(blob), i + 1
                    except json.JSONDecodeError:
                        return None, i + 1
    return None, len(text)


def extract_json_object(text: str) -> dict:
    """First balanced object in `text` that parses as JSON."""
    if not text or not text.strip():
        raise ModelParseError("empty model output")
    pos = 0
    last_error = None
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        obj, end = _extract_first_balanced(text, start)
        if obj is not None:
            return obj
        last_error = text[start:min(end, len(text))][:200]
        pos = end if end > start else start + 1
    if last_error is not None:
        raise ModelParseError(
            f"model returned no valid JSON object; nearest={last_error!r}"
        )
    raise ModelParseError(f"no JSON object in model output: {text[:200]!r}")
