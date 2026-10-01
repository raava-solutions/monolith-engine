"""Exercise collection/setup without touching a real daemon."""
from pathlib import Path
pytest_plugins = ["pytester"]

def test_live_setup_requires_explicit_opt_in(pytester):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text())
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers = live_docker: live service test\n")
    pytester.makepyfile('''
import pytest
@pytest.fixture(autouse=True)
def daemon_probe():
    raise RuntimeError("daemon was contacted")
@pytest.mark.live_docker
def test_live():
    pass
''')
    pytester.runpytest("-q").assert_outcomes(skipped=1)
    result = pytester.runpytest("-q", "--live-docker")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*RuntimeError: daemon was contacted*"])
