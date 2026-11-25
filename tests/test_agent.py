"""The agent, end to end, with a real model. Each test checks the outcome in the sandbox."""

import json

import pytest

from computer_use import Desktop
from computer_use.agent import run_task
from conftest import SITE, log_step

pytestmark = pytest.mark.agent


def test_fills_and_submits_a_web_form(clean_desktop: Desktop, model: str) -> None:
    result = run_task(
        clean_desktop,
        f"Go to {SITE}, start a free trial, and create an account for Ada Lovelace "
        "with the email ada@example.com on the Pro plan.",
        model=model,
        on_step=log_step,
    )
    assert result.finished, result.answer
    signups = [json.loads(line) for line in clean_desktop.run("cat /tmp/test-site/signups.jsonl").splitlines()]
    assert signups == [{"name": "Ada Lovelace", "email": "ada@example.com", "plan": "Pro", "terms": "yes"}]


def test_uses_the_desktop_with_only_mouse_and_keyboard(clean_desktop: Desktop, model: str) -> None:
    result = run_task(
        clean_desktop,
        "Using the desktop's right-click menu, create a new folder named invoices on the desktop.",
        model=model,
        browser_tools=False,
        on_step=log_step,
    )
    assert result.finished, result.answer
    clean_desktop.run("test -d /home/cua/Desktop/invoices")


def test_asks_the_user_for_a_one_time_code(clean_desktop: Desktop, model: str) -> None:
    questions: list[str] = []

    def ask_user(question: str) -> str:
        questions.append(question)
        return "The code is 481516."

    result = run_task(
        clean_desktop,
        f"Sign in at {SITE}/login with the email ada@example.com.",
        model=model,
        ask_user=ask_user,
        on_step=log_step,
    )
    assert result.finished, result.answer
    assert questions, "The agent should ask the user for the code instead of guessing."
    assert json.loads(clean_desktop.run("cat /tmp/test-site/logins.jsonl")) == {"email": "ada@example.com"}


def test_looks_something_up_on_the_web(clean_desktop: Desktop, model: str) -> None:
    result = run_task(
        clean_desktop,
        "On Wikipedia, find the year the Eiffel Tower was completed. Answer with just the year.",
        model=model,
        on_step=log_step,
    )
    assert result.finished
    assert "1889" in result.answer
