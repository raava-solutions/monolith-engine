# monolith-engine

The open single-node engine for Monolith. Runs core agent-infrastructure
primitives (container provider, local storage, chat-exec) **in-process with
no server**. The open CLI imports it for local; the closed hosted service
imports the same engine for cloud, supplying its own provider/storage/secrets
backends behind identical ports.

Apache-2.0.


## Verification

Install development dependencies with `python -m pip install -e '.[dev]'`,
then run `python -m pytest -q --strict-markers`. The default suite uses test
fakes and temporary SQLite databases. It does not probe Docker or create
containers, even when a daemon is available. CI runs this offline suite on
Python 3.11 and 3.12.

To deliberately run the real Docker round trips on a development machine:
`python -m pytest tests/test_docker_provider_live.py --live-docker -q`.
These tests may pull `ubuntu:24.04` and create/delete temporary containers.

Workflow cancellation is cooperative: an in-flight command is not killed,
but cancellation is checked before subsequent retry attempts. Re-entering an
already terminal run returns its recorded status without repeating effects.
This is not an exactly-once guarantee for commands interrupted before their
result is committed; those commands must still be safe to retry.

Run `python scripts/check_complexity.py` before submitting changes. CI enforces
Ruff C901 cyclomatic complexity: new functions must score at most 10; legacy
functions cannot exceed `quality/complexity-baseline.json`. Reductions must
lower/remove the corresponding entry. Inline `noqa` and repository lint
suppression cannot bypass this check. Baseline increases require explicit
maintainer review; they are not a fix for a failing check. Refactor around
cohesive responsibilities, preserving behavior with focused contract tests.
Cognitive complexity is not measured by this gate.
