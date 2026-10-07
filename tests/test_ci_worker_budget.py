"""Auto workers must fit the actual container, including explicit CI defaults."""
import pytest

from tests import conftest


@pytest.mark.parametrize("quota,ci,requested,expected", [
    (4, "1", "16", "4"),
    (4, "1", "auto", "4"),
    (None, "", "auto", "auto"),
    (4, "1", "2", "2"),
    (4, "", None, "4"),
    (None, "1", "16", "8"),
    (None, "1", "4", "4"),
    (None, "", "16", "16"),
    (None, "", None, None),
])
def test_auto_worker_budget(monkeypatch, quota, ci, requested, expected):
    monkeypatch.setattr(conftest, "_cgroup_cpu_quota", lambda: quota)
    monkeypatch.setenv("CI", ci)
    key = "PYTEST_XDIST_AUTO_NUM_WORKERS"
    if requested is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, requested)
    conftest._configure_xdist_workers()
    assert conftest.os.environ.get(key) == expected


def test_worker_budget_at_startup(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    from tests._sync import SYNC_TIMEOUT

    # Exercise xdist startup with a larger runner, isolated from repo fixtures.
    (tmp_path / "conftest.py").write_text(
        "def pytest_xdist_auto_num_workers(config):\n    return 16\n"
    )
    probe = tmp_path / "test_count.py"
    probe.write_text(
        "def test_count(request):\n"
        "    assert request.config.workerinput['workercount'] == 4\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-c",
         str(Path(__file__).resolve().parents[1] / "pytest.ini"),
         "--confcutdir", str(tmp_path), str(probe)],
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=SYNC_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
