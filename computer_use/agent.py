"""An agent that uses a Desktop the way a person does: screen, mouse, keyboard, and browser.

The loop runs in your backend. Each turn it sends the model the task, the
conversation so far, and a fresh screenshot; the model answers with tool
calls; the loop runs them on the desktop and repeats until the model calls
``done``. Any OpenAI-compatible chat model that takes images and tools works.
The default is a vision model on Baseten Model APIs.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import OpenAI

from computer_use.desktop import SCREEN_HEIGHT, SCREEN_WIDTH, Desktop

DEFAULT_MODEL = "moonshotai/Kimi-K3.1-Pro"
BASETEN_INFERENCE_URL = "https://inference.baseten.co/v1"

SYSTEM_PROMPT = f"""You are a computer-use agent. You operate a Linux computer \
with an XFCE desktop ({SCREEN_WIDTH}x{SCREEN_HEIGHT} pixels) and a Google \
Chrome browser on behalf of a user, who may be watching the screen live.

How to work:
- After each turn you get a fresh screenshot of the whole screen. Look at it \
before deciding the next step, and check that your last step worked.
- For web pages, prefer the browser tools. browser_snapshot lists the page's \
elements with refs like e12, and browser_click, browser_type and \
browser_select act on those refs. They are faster and more reliable than \
clicking pixels. Refs change when the page changes, so take a new snapshot \
after navigating.
- For everything else (the desktop, other apps, menus, dialogs, the \
browser's own toolbar), use the computer tool with pixel coordinates from \
the latest screenshot. (0, 0) is the top-left corner.
- Take a few steps at a time, then look again.
- If you need something only the user can provide or approve (a one-time \
code, a CAPTCHA, confirming a purchase or a message to someone), call \
ask_user. Never ask for a password: when a sign-in needs one, use ask_user \
to ask the user to type it on the screen themselves, and continue when they \
say they are done. Don't go looking for passwords, codes or other secrets the \
user hasn't given you, in files, terminals, page source or anywhere else.
- When the task is complete, call done with a short answer for the user. If \
it can't be completed, call done and say why."""

_COMPUTER_TOOL = {
    "type": "function",
    "function": {
        "name": "computer",
        "description": "Use the mouse and keyboard. Coordinates are pixels in the latest screenshot.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "double_click", "right_click", "move", "drag", "type", "key", "scroll", "wait"],
                },
                "x": {"type": "integer", "description": "Pixel x for click, move, drag start, and scroll."},
                "y": {"type": "integer", "description": "Pixel y for click, move, drag start, and scroll."},
                "to_x": {"type": "integer", "description": "drag: end x."},
                "to_y": {"type": "integer", "description": "drag: end y."},
                "text": {"type": "string", "description": "type: the text to type."},
                "keys": {
                    "type": "string",
                    "description": "key: one key or a combination, such as Return, Tab, Escape, ctrl+l, ctrl+shift+t.",
                },
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "description": "scroll: wheel clicks. Defaults to 3."},
                "seconds": {"type": "number", "description": "wait: seconds. Defaults to 1."},
            },
            "required": ["action"],
        },
    },
}


def _function(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


_BROWSER_TOOLS = [
    _function("browser_open", "Open a URL in the browser tab in front.", {"url": {"type": "string"}}, ["url"]),
    _function("browser_snapshot", "List the current page's elements, with refs to act on."),
    _function("browser_click", "Click a page element by ref.", {"ref": {"type": "string"}}, ["ref"]),
    _function(
        "browser_type",
        "Replace the text in a field, found by ref. Set submit to press Enter after.",
        {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
        ["ref", "text"],
    ),
    _function(
        "browser_select",
        "Choose an option in a dropdown, found by ref, by its visible label.",
        {"ref": {"type": "string"}, "option": {"type": "string"}},
        ["ref", "option"],
    ),
    _function("browser_read", "Read the visible text of the current page."),
]

_CONTROL_TOOLS = [
    _function(
        "ask_user",
        "Ask the user for something only they can provide or approve. Returns their reply.",
        {"question": {"type": "string"}},
        ["question"],
    ),
    _function(
        "done",
        "Finish the task and give the user a short answer.",
        {"answer": {"type": "string"}},
        ["answer"],
    ),
]

_MAX_TOOL_OUTPUT = 15_000
_SCREENSHOTS_KEPT = 3


@dataclass
class Step:
    """One action the agent took, for progress logs and transcripts."""

    number: int
    tool: str
    arguments: dict
    result: str


@dataclass
class Result:
    answer: str
    finished: bool
    steps: list[Step] = field(default_factory=list)
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def run_task(
    desktop: Desktop,
    task: str,
    *,
    model: str = DEFAULT_MODEL,
    client: OpenAI | None = None,
    browser_tools: bool = True,
    max_turns: int = 40,
    on_step: Callable[[Step], None] | None = None,
    ask_user: Callable[[str], str] | None = None,
    approve: Callable[[str, dict], bool] | None = None,
) -> Result:
    """Have the model complete ``task`` on ``desktop`` and return its answer.

    ``client`` defaults to Baseten Model APIs with ``BASETEN_API_KEY``. Pass
    any OpenAI-compatible client to use another provider. With
    ``browser_tools=False`` the agent works only through the screen, mouse,
    and keyboard. ``ask_user`` answers the model's questions; without it, the
    model is told the user isn't available. ``approve(tool, arguments)`` runs
    before every action on the desktop or browser; return ``False`` to block
    it. Use it to enforce approvals in your app rather than only in the prompt.
    ``on_step`` runs after each action, for progress.
    """
    client = client or OpenAI(base_url=BASETEN_INFERENCE_URL, api_key=os.environ["BASETEN_API_KEY"])
    tools = [_COMPUTER_TOOL, *(_BROWSER_TOOLS if browser_tools else []), *_CONTROL_TOOLS]
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        _screen_message(desktop, f"Task: {task}\n\nThis is the screen now."),
    ]
    result = Result(answer="", finished=False)
    started = time.monotonic()

    for _ in range(max_turns):
        response = client.chat.completions.create(model=model, messages=messages, tools=tools, max_tokens=8192)
        if response.usage:
            result.input_tokens += response.usage.prompt_tokens
            result.output_tokens += response.usage.completion_tokens
        message = response.choices[0].message
        messages.append(_assistant_message(message))

        if not message.tool_calls:
            messages.append(_screen_message(desktop, "Use a tool to continue, or call done if the task is complete."))
            continue

        for call in message.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if name == "done":
                result.answer, result.finished = str(arguments.get("answer", "")), True
                output = "Done."
            elif name not in ("ask_user",) and approve is not None and not approve(name, arguments):
                output = "The user did not allow this action. Don't retry it; ask the user or call done."
            else:
                output = _run_tool(desktop, name, arguments, ask_user)
            step = Step(number=len(result.steps) + 1, tool=name, arguments=arguments, result=output)
            result.steps.append(step)
            if on_step:
                on_step(step)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output[:_MAX_TOOL_OUTPUT]})
            if result.finished:
                break  # Run nothing the model queued after done.

        if result.finished:
            break
        time.sleep(0.5)  # Let the screen settle after the last action.
        messages.append(_screen_message(desktop, "This is the screen now."))
        _drop_old_screenshots(messages)

    result.seconds = time.monotonic() - started
    if not result.finished:
        result.answer = f"Stopped after {max_turns} turns without finishing."
    return result


def _run_tool(desktop: Desktop, name: str, args: dict, ask_user: Callable[[str], str] | None) -> str:
    try:
        if name == "computer":
            return _computer(desktop, args)
        if name == "ask_user":
            if ask_user is None:
                return "The user isn't available. Continue without them, or call done and say what you need."
            return ask_user(str(args.get("question", "")))
        if name.startswith("browser_"):
            return _browser(desktop, name, args)
        return f"Unknown tool {name}."
    except Exception as error:  # Report failures to the model so it can recover.
        return f"Error: {type(error).__name__}: {error}"[:2000]


def _computer(desktop: Desktop, args: dict) -> str:
    action = args.get("action")
    x, y = args.get("x"), args.get("y")
    if action in ("click", "double_click", "right_click", "move", "drag", "scroll") and (x is None or y is None):
        return f"Error: {action} needs x and y."
    if action == "click":
        desktop.click(int(x), int(y))
    elif action == "double_click":
        desktop.click(int(x), int(y), clicks=2)
    elif action == "right_click":
        desktop.click(int(x), int(y), button="right")
    elif action == "move":
        desktop.move(int(x), int(y))
    elif action == "drag":
        desktop.drag(int(x), int(y), int(args["to_x"]), int(args["to_y"]))
    elif action == "type":
        desktop.type_text(str(args.get("text", "")))
    elif action == "key":
        desktop.press(str(args.get("keys", "")))
    elif action == "scroll":
        desktop.scroll(int(x), int(y), str(args.get("direction", "down")), int(args.get("amount") or 3))
    elif action == "wait":
        time.sleep(min(float(args.get("seconds") or 1), 10))
    else:
        return f"Error: unknown action {action}."
    return "OK"


def _browser(desktop: Desktop, name: str, args: dict) -> str:
    page = desktop.page()
    if name == "browser_open":
        page.goto(str(args["url"]), wait_until="domcontentloaded", timeout=30_000)
        return f"Opened {page.url} ({page.title()})."
    if name == "browser_snapshot":
        return f"URL: {page.url}\nTitle: {page.title()}\n\n{page.aria_snapshot(mode='ai')}"
    if name == "browser_read":
        return f"URL: {page.url}\nTitle: {page.title()}\n\n{page.inner_text('body')}"
    element = page.locator(f"aria-ref={args['ref']}")
    desktop.point_at(element)
    if name == "browser_click":
        element.click(timeout=10_000)
    elif name == "browser_type":
        text = str(args.get("text", ""))
        if desktop.human_pace:
            element.fill("", timeout=10_000)
            element.press_sequentially(text, delay=max(5, min(35, 1500 // max(len(text), 1))))
        else:
            element.fill(text, timeout=10_000)
        if args.get("submit"):
            element.press("Enter")
    elif name == "browser_select":
        element.select_option(label=str(args["option"]), timeout=10_000)
    else:
        return f"Unknown tool {name}."
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    return f"OK. Page: {page.url}"


def _screen_message(desktop: Desktop, text: str) -> dict:
    image = base64.b64encode(desktop.screenshot()).decode()
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ],
    }


def _assistant_message(message) -> dict:
    entry: dict = {"role": "assistant", "content": message.content or ""}
    # Reasoning models expect their reasoning back alongside their tool calls.
    reasoning = getattr(message, "reasoning_content", None) or (message.model_extra or {}).get("reasoning_content")
    if reasoning:
        entry["reasoning_content"] = reasoning
    if message.tool_calls:
        entry["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in message.tool_calls
        ]
    return entry


def _drop_old_screenshots(messages: list[dict]) -> None:
    """Keep only the latest screenshots, so each turn's request stays small."""
    seen = 0
    for message in reversed(messages):
        content = message.get("content")
        if message["role"] != "user" or not isinstance(content, list):
            continue
        if any(part.get("type") == "image_url" for part in content):
            seen += 1
            if seen > _SCREENSHOTS_KEPT:
                message["content"] = [p for p in content if p.get("type") != "image_url"] + [
                    {"type": "text", "text": "(older screenshot removed)"}
                ]
