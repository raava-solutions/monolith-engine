"""Ensure the ratchet rejects regressions, stale debt and suppressed violations."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "complexity_policy", Path(__file__).parents[1] / "scripts/check_complexity.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def test_new_complex_functions_and_increased_debt_fail():
    assert policy.violations({"new": 11}, {})
    assert policy.violations({"old": 12}, {"old": 11})
    assert not policy.violations({"old": 11}, {"old": 11})


def test_reduced_or_removed_debt_requires_lowering_baseline():
    assert policy.violations({"old": 11}, {"old": 12})
    assert policy.violations({}, {"old": 12})


def test_scanner_ignores_noqa_and_repository_rule_suppression(tmp_path, monkeypatch):
    (tmp_path / "monolith_engine").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nignore = ["C901"]\n')
    code = "class Example:\n    def branch(self, value):  # noqa: C901\n"
    code += "".join(
        f"        if value == {n}:\n            return {n}\n" for n in range(11)
    )
    (tmp_path / "monolith_engine/example.py").write_text(code)
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    assert policy.scores() == {"monolith_engine/example.py::Example.branch": 12}
