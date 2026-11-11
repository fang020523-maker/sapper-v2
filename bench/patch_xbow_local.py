#!/usr/bin/env python3
"""Make xbow challenge Dockerfiles buildable with locally available bases.

The stock files assume Docker Hub at full speed; locally we have
python:3.12-slim cached and PyPI is fastest through the Tsinghua mirror.
This patches FROM lines and pip installs in place (the benchmarks tree
is a local working copy, not the upstream repo).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

FROM_SUBST = {
    "python:3.12": "python:3.12-slim",
    "python:3.11": "python:3.12-slim",
    "python:3.10": "python:3.12-slim",
}


def patch(root: Path) -> None:
    for dockerfile in root.rglob("Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        original = text
        for old, new in FROM_SUBST.items():
            text = re.sub(rf"^FROM {re.escape(old)}(\s|$)", f"FROM {new}\\1", text, flags=re.M)
        if "tuna.tsinghua" not in text:
            text = re.sub(
                r"pip install(?! -i)",
                f"pip install -i {PIP_MIRROR}",
                text,
            )
        if text != original:
            dockerfile.write_text(text, encoding="utf-8")
            print(f"patched {dockerfile}")


if __name__ == "__main__":
    patch(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
