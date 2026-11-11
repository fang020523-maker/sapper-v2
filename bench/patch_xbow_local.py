#!/usr/bin/env python3
"""Make xbow challenge Dockerfiles buildable with locally available bases.

The stock files assume Docker Hub at full speed; locally we have
python:3.12-slim and php:7.4-apache cached, and PyPI is fastest through
the Tsinghua mirror. Patches are in place (the benchmarks tree is a
local working copy, not the upstream repo).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# old FROM -> (new FROM, extra RUN lines inserted right after FROM)
FROM_SUBST = {
    "python:3.12": ("python:3.12-slim", []),
    "python:3.11": ("python:3.12-slim", []),
    "python:3.10": ("python:3.12-slim", []),
    "python:3.9": ("python:3.12-slim", []),
    "tiangolo/uvicorn-gunicorn-fastapi:python3.9": (
        "python:3.12-slim",
        [
            "RUN pip install --no-cache-dir -i " + PIP_MIRROR + " fastapi uvicorn",
            "WORKDIR /app",
        ],
    ),
}


def patch(root: Path) -> None:
    for dockerfile in root.rglob("Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        original = text
        for old, (new, injects) in FROM_SUBST.items():
            pattern = "^(FROM " + re.escape(old) + ")(\\s.*)?$"
            match = re.search(pattern, text, flags=re.M)
            if not match:
                continue
            replacement = "FROM " + new + (match.group(2) or "")
            if injects:
                replacement += "\n" + "\n".join(injects)
            text = text[: match.start()] + replacement + text[match.end() :]
        if "tuna.tsinghua" not in text:
            text = re.sub(r"pip install(?! -i)", "pip install -i " + PIP_MIRROR, text)
        if text != original:
            dockerfile.write_text(text, encoding="utf-8")
            print("patched", dockerfile)


if __name__ == "__main__":
    patch(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
