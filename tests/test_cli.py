"""The computer-use command: usage errors, and one --json run on the test desktop."""

import json
import os
import subprocess
import sys

import pytest

from computer_use import Desktop
from conftest import SITE


def cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "computer_use", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,  # No terminal, as for a coding agent or a CI job.
        env=env,
        timeout=600,
    )


def test_no_task_is_a_usage_error() -> None:
    result = cli(env={**os.environ, "BASETEN_API_KEY": "unused"})
    assert result.returncode == 2
    assert "give a task" in result.stderr


def test_no_api_key_is_a_usage_error() -> None:
    env = {k: v for k, v in os.environ.items() if k != "BASETEN_API_KEY"}
    result = cli("Open example.com.", env=env)
    assert result.returncode == 2
    assert "BASETEN_API_KEY" in result.stderr


@pytest.mark.agent
def test_json_run_without_a_terminal(clean_desktop: Desktop) -> None:
    # The sign-in needs a one-time code that only a person has. With no terminal,
    # the agent is told nobody is available, so it must stop without signing in.
    result = cli("--json", "--desktop", clean_desktop.name, f"Sign in at {SITE}/login with the email ada@example.com.")
    assert result.returncode in (0, 1), result.stderr
    out = json.loads(result.stdout)
    assert set(out) == {"desktop", "answer", "finished", "steps", "seconds", "input_tokens", "output_tokens"}
    assert out["desktop"] == clean_desktop.name
    assert "nobody is available" in result.stderr.lower() or "no terminal" in result.stderr.lower()
    clean_desktop.run("test ! -e /tmp/test-site/logins.jsonl")
