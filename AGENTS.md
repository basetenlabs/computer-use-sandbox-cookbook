# AGENTS.md

Instructions for coding agents that use or change this repo. People can start
with [README.md](README.md).

## What this is

A Python reference for computer-use agents on Baseten Sandboxes. `Desktop`
(`computer_use/desktop.py`) is one user's Linux desktop in a sandbox, and
`run_task` (`computer_use/agent.py`) drives it with a vision model on Baseten
Model APIs. Everything runs against real sandboxes and real models. There are
no mocks.

## Set up

1. Install Python 3.11 or later and [uv](https://docs.astral.sh/uv/), then run
   `uv sync`.
2. Set `BASETEN_API_KEY` to a Baseten API key with access to Sandboxes and
   Model APIs. Never print the key, log it, or write it to a file.
3. Build the desktop image once per Baseten team:
   `uv run computer-use-image`. It takes about 2 minutes the first time and
   seconds after, and it is safe to run again. Run it whenever a command says
   that `computer-use-desktop:latest` isn't in your team's images.

## Run a task

```sh
uv run computer-use --json --desktop my-desktop "On Wikipedia, find the year the Golden Gate Bridge opened. Answer with just the year." > result.json
```

* stdout gets one JSON object: `desktop`, `answer`, `finished`, `steps`,
  `seconds`, `input_tokens` and `output_tokens`. Progress goes to stderr: the
  live view link, then one line per action.
* The exit code is 0 when the agent ended with an answer, 1 when it ran out of
  turns or the desktop failed, and 2 for a usage error. An answer can still
  say the task wasn't possible, for example when it needed a person, so read
  `answer`.
* A new desktop takes about a minute. The same `--desktop` name reconnects to
  that desktop in about 2 seconds, with its windows, files and browser
  sessions as they were.
* With no terminal attached, a question from the agent gets the answer that
  nobody is available. Give it tasks that don't need a person, or call
  `run_task` from Python with your own `ask_user`.
* The live view link is a credential: anyone holding it can use the desktop
  until it expires. Keep it out of shared logs, issues and chats.
* Delete a desktop with `uv run computer-use --delete --desktop my-desktop`.
  Desktops also delete themselves after 2 hours without use.

## Check your work

```sh
uv run pytest -m "not agent"   # the desktop itself, no model calls
uv run pytest                  # everything, including agent tests that spend model tokens
```

* Each run creates a new desktop and deletes it at the end. Set
  `CUA_TEST_DESKTOP=<name>` to reuse a desktop you keep, and
  `CUA_TEST_MODELS=<model>,<model>` to choose the models for the agent tests.
* Tests check outcomes inside the sandbox with `desktop.run(...)`, never the
  model's answer. Write new tests the same way.
* After changing `computer_use/image.py`, rebuild with
  `uv run computer-use-image`, then run the whole suite.

## Layout

| Path | What it holds |
| --- | --- |
| `computer_use/desktop.py` | `Desktop`: create or reconnect, screenshots, mouse and keyboard, the browser through Playwright, live view links, `run` for app code. |
| `computer_use/agent.py` | `run_task`: the agent loop, its tools and the system prompt. |
| `computer_use/image.py` | The desktop image and the `computer-use-image` command. |
| `computer_use/__main__.py` | The `computer-use` command. |
| `computer_use/cdp_forward.py` | Forwards Chrome's DevTools port so it sits behind the sandbox's token-checked port route. |
| `tests/` | End-to-end tests and the small website they serve inside the sandbox (`tests/site/server.py`). |

## Rules for changes

* One sandbox per end user. Never share a sandbox between users.
* `desktop.run` runs as root and is for application code. Never give the model
  a shell tool, and never pass `desktop.run` through to it.
* Gate consequential actions (purchases, messages, deletions) in code with
  `approve=` on `run_task`, not only in the prompt.
* Keep secrets out of the desktop user's reach and off command lines: the
  sandbox's process list shows `desktop.run` commands. Write secrets to a
  file only root can read.
* `BASE_IMAGE` in `image.py` is pinned by digest on purpose. Move the pin only
  together with a full test run.
* `Desktop` uses Playwright's sync API. Open, use and close each `Desktop` on
  one thread, and outside any asyncio event loop.
