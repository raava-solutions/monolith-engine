"""Ensure the ratchet rejects regressions, stale debt and suppressed violations."""

import importlib.util
from pathlib import Path

import pytest

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


def conditional_source(first_name="first", first_branches=11):
    def function(name, count):
        return f"    def {name}(value):\n" + "".join(
            f"        if value == {n}:\n            return {n}\n" for n in range(count)
        )

    return (
        "if True:\n"
        + function(first_name, first_branches)
        + "else:\n"
        + function("second", 11)
    )


def test_conditional_symbols_cannot_hide_increased_complexity(tmp_path, monkeypatch):
    (tmp_path / "monolith_engine").mkdir()
    (tmp_path / "scripts").mkdir()
    path = tmp_path / "monolith_engine/example.py"
    path.write_text(conditional_source())
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    baseline = policy.scores()
    assert baseline == {"monolith_engine/example.py::first": 12, "monolith_engine/example.py::second": 12}
    path.write_text(conditional_source(first_branches=30))
    measured = policy.scores()
    assert measured["monolith_engine/example.py::first"] == 31
    assert policy.violations(measured, baseline)


def test_duplicate_conditional_symbols_fail_closed(tmp_path, monkeypatch):
    (tmp_path / "monolith_engine").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "monolith_engine/example.py").write_text(conditional_source(first_name="second"))
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="Duplicate complexity symbol"):
        policy.scores()


def test_qualified_names_cross_try_and_if_wrappers(tmp_path):
    path = tmp_path / "wrapped.py"
    path.write_text(
        "try:\n    class Example:\n        if True:\n            async def method():\n                pass\nexcept Exception:\n    pass\n"
    )
    assert policy.qualified_function(path, 4) == "Example.method"
