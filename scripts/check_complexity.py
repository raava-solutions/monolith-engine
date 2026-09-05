"""Enforce a C901 debt ceiling without accepting inline lint suppressions.

The reviewed baseline contains legacy functions only. New functions must be <=10;
existing functions cannot exceed their recorded score. Reductions require lowering
or removing their baseline entry. Ruff is pinned in the development dependencies.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "quality/complexity-baseline.json"


def qualified_function(path: Path, line: int) -> str:
    tree = ast.parse(path.read_text())
    names = []
    node = tree
    while True:
        candidates = [
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and child.lineno <= line <= child.end_lineno
        ]
        if not candidates:
            return ".".join(names)
        node = candidates[0]
        names.append(node.name)


def scores() -> dict[str, int]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "monolith_engine",
            "scripts",
            "--isolated",
            "--select",
            "C901",
            "--ignore-noqa",
            "--output-format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    measured = {}
    for item in json.loads(result.stdout):
        path = Path(item["filename"])
        name = qualified_function(path, item["location"]["row"])
        score = int(re.search(r"\((\d+) >", item["message"]).group(1))
        measured[f"{path.relative_to(ROOT).as_posix()}::{name}"] = score
    return measured


def violations(measured: dict[str, int], baseline: dict[str, int]) -> list[str]:
    errors = [
        f"{name}: complexity {score} exceeds ceiling {baseline.get(name, 10)}"
        for name, score in measured.items()
        if score > baseline.get(name, 10)
    ]
    errors.extend(
        f"{name}: lower/remove stale baseline {score}; current {measured.get(name, '<=10')}"
        for name, score in baseline.items()
        if measured.get(name, 10) < score
    )
    return errors


def main() -> int:
    baseline = json.loads(BASELINE.read_text())["ceilings"]
    errors = violations(scores(), baseline)
    print(
        "\n".join(errors)
        if errors
        else "Complexity ratchet passed (new functions <=10)."
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
