"""Fixtures for the end-to-end tests. They run against real Baseten Sandboxes.

Set BASETEN_API_KEY. The session creates one desktop and deletes it at the
end. Set CUA_TEST_DESKTOP to reuse a desktop you keep (it is not deleted), and
CUA_TEST_MODELS to a comma-separated list of models for the agent tests.
"""

import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from computer_use import Desktop
from computer_use.agent import DEFAULT_MODEL, Step

SITE_PORT = 3000
SITE = f"http://localhost:{SITE_PORT}"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "model" in metafunc.fixturenames:
        models = [m.strip() for m in os.environ.get("CUA_TEST_MODELS", DEFAULT_MODEL).split(",") if m.strip()]
        metafunc.parametrize("model", models)


@pytest.fixture(scope="session")
def desktop():
    if not os.environ.get("BASETEN_API_KEY"):
        pytest.skip("Set BASETEN_API_KEY to run the end-to-end tests.")
    kept = os.environ.get("CUA_TEST_DESKTOP")
    desktop = Desktop.open(kept or f"cua-test-{uuid.uuid4().hex[:8]}", idle_timeout=timedelta(minutes=30))
    try:
        desktop.sandbox.fs.write(
            path="/opt/test-site/server.py", content=(Path(__file__).parent / "site" / "server.py").read_text()
        )
        # The source holds the one-time code. Keep it from the desktop's user, so
        # the agent can't find the code on the computer and has to ask.
        desktop.run("chmod 700 /opt/test-site")
        if "test-site" not in {p.name for p in desktop.sandbox.process.list() if p.status == "running"}:
            desktop.sandbox.process.exec(
                command=f"python3 /opt/test-site/server.py {SITE_PORT}",
                name="test-site",
                timeout=timedelta(0),
                wait_for_ports=[SITE_PORT],
            )
        yield desktop
    finally:
        if not kept:
            desktop.delete()
        desktop.close()


@pytest.fixture
def clean_desktop(desktop: Desktop) -> Desktop:
    """The session's desktop with the test site's records and the desktop folder emptied."""
    desktop.run("rm -rf /tmp/test-site/*.jsonl /home/cua/Desktop/*; true")
    desktop.page().goto("about:blank")
    return desktop


def log_step(step: Step) -> None:
    print(f"  {step.number:>2}. {step.tool} {step.arguments} -> {step.result[:120]!r}")
