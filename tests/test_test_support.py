"""One session plugin reports both repositories without changing fixture scope."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("layout", ["component", "parent"])
def test_shared_session_plugin_reports_once_and_preserves_repo_roots(tmp_path, layout):
    machine = tmp_path / "airlock-machine"
    tests = machine / "tests"
    tests.mkdir(parents=True)
    shutil.copytree(ROOT / "tests/airlock_test_support", tests / "airlock_test_support")
    shutil.copy(ROOT / "tests/conftest.py", tests / "conftest.py")
    (tests / "test_scope.py").write_text(
        "def test_scope(repo_root):\n    assert repo_root.name == 'airlock-machine'\n"
    )
    selected = [str(tests)]
    if layout == "parent":
        parent = tmp_path / "tests"
        parent.mkdir()
        (parent / "conftest.py").write_text(
            "from pathlib import Path\nimport sys\nimport pytest\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "airlock-machine/tests"))\n'
            "from airlock_test_support import plugin\n"
            "def pytest_configure(config):\n"
            '    if not config.pluginmanager.hasplugin("airlock-session"):\n'
            '        config.pluginmanager.register(plugin, "airlock-session")\n'
            "@pytest.fixture\ndef repo_root():\n"
            "    return Path(__file__).resolve().parents[1]\n"
        )
        (parent / "test_parent_scope.py").write_text(
            f"def test_scope(repo_root):\n    assert str(repo_root) == {str(tmp_path)!r}\n"
        )
        selected.append(str(parent))
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    logs = tmp_path / "logs"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", str(config), *selected],
        cwd=tmp_path,
        env=dict(
            os.environ,
            AIRLOCK_PYTEST_ENV_LOADED="1",
            AIRLOCK_PYTEST_WARM_STORE="0",
            AIRLOCK_LOG_DIR=str(logs),
            PYTEST_ADDOPTS="",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = (logs / "test-runtimes-history.jsonl").read_text().splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["test_count"] == len(selected)
