"""SC4 guard — the open engine must not import any hosted/cloud dependency.

A regression here means the carve leaked a multi-tenant/cloud import into the
open package, breaking the "engine has zero hosted deps" success criterion.
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN_PREFIXES = (
    "fleet",           # the hosted repo's package
    "services",        # hosted services
    "routers",         # hosted HTTP layer
    "auth",            # hosted multi-tenant auth
    "db",              # hosted Postgres god-module
    "asyncpg",         # Postgres driver
    "google.cloud",    # GSM / GCE
    "fastapi",         # the hosted HTTP framework
)

_PKG = pathlib.Path(__file__).resolve().parents[1] / "monolith_engine"


def _imports(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name, path
        elif isinstance(node, ast.ImportFrom):
            yield (node.module or ""), path


def test_engine_has_no_hosted_imports():
    leaks = []
    for py in _PKG.rglob("*.py"):
        for module, path in _imports(py):
            if any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                leaks.append(f"{path.relative_to(_PKG.parent)} imports {module}")
    assert not leaks, "Hosted imports leaked into the open engine:\n" + "\n".join(leaks)
