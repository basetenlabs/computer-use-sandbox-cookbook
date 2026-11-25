"""A user's cloud computer on Baseten Sandboxes.

One sandbox is one computer: an XFCE desktop at 1024x768, a visible Google
Chrome on it, and a live view a person can watch or take over from their own
browser. It runs the ``computer-use-desktop`` image, which
``computer-use-image`` builds from Baseten's built-in ``baseten/cua-xfce``
desktop (see ``computer_use/image.py``). Your backend
drives it over HTTPS:

* Mouse, keyboard, and screenshots go to the image's computer server on port
  8000, through the sandbox's ``/port/8000/`` route.
* Browser control goes to Chrome's DevTools (CDP) through
  ``/port/9223/``, so Playwright, or any CDP client, drives the same browser
  window the person sees.
* The live view is noVNC on port 6901, behind a private preview URL.

Every route requires a sandbox access token or a preview token. Nothing is
public. A live view link carries its preview token, and the VNC server behind
it takes input from every viewer, so anyone holding a link can use the desktop.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from baseten.sandbox import (
    Sandbox,
    SandboxClient,
    SandboxExpirationPolicyIdle,
    SandboxLifecycle,
)
from playwright.sync_api import Browser, Locator, Page, Playwright, sync_playwright

IMAGE = "computer-use-desktop:latest"  # Built by computer-use-image.
BUILTIN_IMAGE = "baseten/cua-xfce:latest"  # Works too, without app menus, icons or human_pace.
SCREEN_WIDTH, SCREEN_HEIGHT = 1024, 768
COMPUTER_SERVER_PORT = 8000
LIVE_VIEW_PORT = 6901
CHROME_CDP_PORT = 9222
FORWARDED_CDP_PORT = 9223
LIVE_VIEW_PREVIEW = "desktop"
API_BASE_URL = "https://api.baseten.co"

_FILES_DIR = "/opt/computer-use"

# Starts Google Chrome on the desktop as the desktop's user, with DevTools on
# 127.0.0.1:9222. On first use it downloads Chrome from Google and unpacks it
# (about 10 seconds, while the desktop is still starting; the image has no
# package database for apt). Bake it into a custom image to skip that.
_START_CHROME = r"""#!/bin/bash
set -euo pipefail
CHROME=/opt/google/chrome/chrome
if [ ! -x "$CHROME" ]; then
  python3.12 -c "import urllib.request; urllib.request.urlretrieve(
    'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb', '/tmp/chrome.deb')"
  rm -rf /tmp/chrome-unpack && dpkg-deb -x /tmp/chrome.deb /tmp/chrome-unpack
  mkdir -p /opt/google && mv /tmp/chrome-unpack/opt/google/chrome /opt/google/chrome
  rm -rf /tmp/chrome.deb /tmp/chrome-unpack
fi
# Start once the window manager and desktop run. A Chrome started before them
# keeps the wrong window frame and can stop drawing after a minimize.
until pgrep -x xfwm4 > /dev/null && pgrep -x xfdesktop > /dev/null; do sleep 0.5; done
exec su cua -c "DISPLAY=:1 exec $CHROME \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/cua/.config/agent-chrome \
  --no-first-run --no-default-browser-check \
  --password-store=basic \
  --disable-dev-shm-usage \
  --disable-features=Translate \
  --start-maximized \
  about:blank"
"""

# Moves the pointer and types the way a person does, for people watching the
# live view or a recording: the pointer glides to its target and text appears
# key by key. Needs xdotool, which the computer-use-desktop image includes.
_PACE = r"""
import math, os, subprocess, sys, time

def xdo(*args):
    return subprocess.run(["xdotool", *args], check=True, capture_output=True, text=True).stdout

def glide(x, y):
    here = dict(part.split("=") for part in xdo("getmouselocation", "--shell").split())
    x0, y0 = int(here["X"]), int(here["Y"])
    distance = math.hypot(x - x0, y - y0)
    if distance < 2:
        return
    seconds = min(0.6, 0.2 + distance / 2000)
    steps = max(8, int(seconds * 60))
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)
        xdo("mousemove", str(round(x0 + (x - x0) * eased)), str(round(y0 + (y - y0) * eased)))
        time.sleep(seconds / steps)

command, args = sys.argv[1], sys.argv[2:]
if command == "glide":
    glide(int(args[0]), int(args[1]))
elif command == "drag":
    glide(int(args[0]), int(args[1]))
    xdo("mousedown", "1")
    glide(int(args[2]), int(args[3]))
    xdo("mouseup", "1")
elif command == "type":
    # The text comes in a file, so it never appears on a command line.
    with open(args[0]) as file:
        text = file.read()
    os.remove(args[0])
    delay = max(5, min(35, 1500 // max(len(text), 1)))
    for i, line in enumerate(text.split("\n")):
        if i:
            xdo("key", "Return")
        if line:
            xdo("type", "--delay", str(delay), "--", line)
"""
_X_ENV = "DISPLAY=:1 XAUTHORITY=/home/cua/.Xauthority"

# Waits until the desktop is drawn and the computer server listens.
_WAIT_FOR_DESKTOP = r"""
for i in $(seq 1 120); do
  if pgrep -x xfdesktop > /dev/null && ss -ltn | grep -q ':8000 '; then sleep 1; exit 0; fi
  sleep 0.5
done
echo "desktop did not start" >&2
exit 1
"""

_WAIT_FOR_CHROME = r"""
for i in $(seq 1 200); do
  if ss -ltn | grep -q '127.0.0.1:9222 '; then exit 0; fi
  sleep 0.5
done
echo "Chrome did not start; see the logs of the chrome process" >&2
exit 1
"""

_KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "control": "ctrl",
    "option": "alt",
    "super": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "meta": "cmd",
    "del": "delete",
    "pageup": "page_up",
    "pagedown": "page_down",
    "pgup": "page_up",
    "pgdn": "page_down",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "spacebar": "space",
    "capslock": "caps_lock",
    "plus": "+",
}


class DesktopError(RuntimeError):
    """A desktop action failed inside the sandbox."""


class _SandboxTokens:
    """Sandbox access tokens for the port routes, renewed before they expire.

    The SDK manages its own tokens; these are for the HTTP and CDP calls this
    module makes to the sandbox's ``/port/<PORT>/`` routes. See
    https://docs.baseten.co/sandboxes/authentication.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def get(self) -> str:
        with self._lock:
            if self._expires_at - datetime.now(timezone.utc) < timedelta(minutes=10):
                response = httpx.post(
                    f"{API_BASE_URL}/v1/token",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"scopes": ["sandboxes"]},
                    timeout=30,
                )
                response.raise_for_status()
                body = response.json()
                self._token = body["token"]
                self._expires_at = datetime.fromisoformat(body["expires_at"])
            return self._token

    def discard(self, token: str) -> None:
        """Forget a token the sandbox rejected. Tokens are revoked when an API key changes."""
        with self._lock:
            if self._token == token:
                self._expires_at = datetime.min.replace(tzinfo=timezone.utc)


class Desktop:
    """One user's cloud computer: a desktop and a browser in a Baseten Sandbox.

    Use :meth:`open` to create one or reconnect to it by name.

    The browser connection uses Playwright's sync API, which belongs to the
    thread that opened it and can't run inside an asyncio event loop. Open,
    use, and close each Desktop on one thread; in an async app, give each
    Desktop its own worker thread.
    """

    def __init__(self, client: SandboxClient, sandbox: Sandbox, api_key: str, *, human_pace: bool = False) -> None:
        self._client = client
        self.sandbox = sandbox
        self.name = sandbox.name
        self.url = sandbox.url.rstrip("/")
        self._tokens = _SandboxTokens(api_key)
        self._http = httpx.Client(timeout=60)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.human_pace = human_pace

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(
        cls,
        name: str,
        *,
        api_key: str | None = None,
        image: str = IMAGE,
        memory_mb: int = 8192,
        idle_timeout: timedelta = timedelta(hours=2),
        human_pace: bool = False,
    ) -> Desktop:
        """Create the desktop named ``name``, or reconnect to it if it exists.

        Returns once the desktop is drawn and the browser accepts DevTools
        connections. ``idle_timeout`` deletes the sandbox after that long
        without requests; while idle before then, it sits on standby, which
        keeps its open windows, files, and browser sessions and bills only
        storage. ``human_pace`` glides the pointer and types key by key, for
        demos and for users who watch the agent work; it costs a fraction of a
        second per action.
        """
        api_key = api_key or os.environ["BASETEN_API_KEY"]
        client = SandboxClient(api_key=api_key)
        try:
            sandbox = client.create(
                name=name,
                create_if_not_exists=True,
                image=image,
                memory=memory_mb,
                labels={"app": "computer-use"},
                lifecycle=SandboxLifecycle(
                    expiration_policies=[SandboxExpirationPolicyIdle(after=idle_timeout)]
                ),
            )
        except BaseException as e:
            client.close()
            if image == IMAGE and "not found" in str(e).lower():
                raise DesktopError(
                    f"The image {IMAGE} isn't in your team's images yet. "
                    "Build it once with: uv run computer-use-image (about 2 minutes)"
                ) from e
            raise
        desktop = cls(client, sandbox, api_key, human_pace=human_pace)
        try:
            # On the built-in image, Chrome downloads while the XFCE session is still coming up.
            desktop.start_browser()
            desktop._wait_for_desktop()
            if human_pace:
                desktop.sandbox.fs.write(path=f"{_FILES_DIR}/pace.py", content=_PACE)
                desktop.run("command -v xdotool > /dev/null || { echo 'human_pace needs xdotool: build the image with computer-use-image' >&2; exit 1; }")
        except BaseException:
            desktop.close()
            raise
        return desktop

    def close(self) -> None:
        """Disconnect. The sandbox keeps running until it expires or is deleted."""
        self._disconnect_browser()
        if self._playwright is not None:
            _release_playwright()
            self._playwright = None
        self._http.close()
        self._client.close()

    def delete(self) -> None:
        """Delete the sandbox and everything on it. Call :meth:`close` after."""
        self._disconnect_browser()
        self._client.delete(name=self.name)

    def __enter__(self) -> Desktop:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- live view -----------------------------------------------------------

    def live_view_url(self, *, expires_in: timedelta = timedelta(hours=1)) -> str:
        """A private link to watch the desktop, or take it over, from any browser.

        Treat the link as a credential: anyone who has it can watch and use the
        desktop until it expires. The VNC server takes input from every viewer,
        so there is no watch-only link; noVNC's ``view_only`` setting only hides
        input in one page. Keep ``expires_in`` short.
        """
        preview = self.sandbox.previews.create(
            name=LIVE_VIEW_PREVIEW, port=LIVE_VIEW_PORT, public=False, create_if_not_exists=True
        )
        token = self.sandbox.previews.create_token(
            preview_name=preview.name, expires_at=datetime.now(timezone.utc) + expires_in
        )
        query = urlencode(
            {
                "autoconnect": "true",
                "reconnect": "true",
                "resize": "scale",
                "preview_token": token.token,
            }
        )
        return f"{preview.url.rstrip('/')}/vnc.html?{query}"

    # -- mouse, keyboard, screen ----------------------------------------------

    def screenshot(self) -> bytes:
        """The whole screen as a PNG, 1024x768."""
        return base64.b64decode(self._command("screenshot")["image_data"])

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._glide(x, y)
        if button == "right":
            self._command("right_click", x=x, y=y)
        elif clicks == 2:
            self._command("double_click", x=x, y=y)
        else:
            for _ in range(clicks):
                self._command("left_click", x=x, y=y)

    def move(self, x: int, y: int) -> None:
        self._glide(x, y)
        self._command("move_cursor", x=x, y=y)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if self.human_pace:
            self._pace("drag", x1, y1, x2, y2)
            return
        self._command("move_cursor", x=x1, y=y1)
        self._command("drag_to", x=x2, y=y2)

    def type_text(self, text: str) -> None:
        if self.human_pace:
            # A file keeps the text off the command line, which the sandbox records.
            path = f"{_FILES_DIR}/typing-{uuid.uuid4().hex}.txt"
            self.sandbox.fs.write(path=path, content=text)
            self._pace("type", path)
            return
        self._command("type_text", text=text)

    def press(self, keys: str) -> None:
        """Press a key or a combination, such as ``Return``, ``ctrl+l``, or ``alt+F4``."""
        parts = [keys] if keys.strip() == "+" else [k for k in keys.split("+") if k.strip()]
        names = [_key_name(k, in_combo=len(parts) > 1) for k in parts]
        if len(names) == 1:
            self._command("press_key", key=names[0])
        else:
            self._command("hotkey", keys=names)

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        self._glide(x, y)
        self._command("move_cursor", x=x, y=y)
        dx, dy = {"up": (0, amount), "down": (0, -amount), "left": (-amount, 0), "right": (amount, 0)}[
            direction
        ]
        self._command("scroll", x=dx, y=dy)

    def point_at(self, element: Locator) -> None:
        """With human_pace, glide the pointer onto a page element before the browser tools act on it."""
        if not self.human_pace:
            return
        element.scroll_into_view_if_needed(timeout=10_000)
        box = element.bounding_box()
        if not box:
            return
        # Where the page's top-left corner sits on the screen, below Chrome's toolbars.
        left, top = element.page.evaluate(
            "() => [window.screenX + (window.outerWidth - window.innerWidth) / 2,"
            " window.screenY + window.outerHeight - window.innerHeight]"
        )
        self._glide(round(left + box["x"] + box["width"] / 2), round(top + box["y"] + box["height"] / 2))

    def _glide(self, x: int, y: int) -> None:
        if self.human_pace:
            self._pace("glide", x, y)

    def _pace(self, *args: object) -> None:
        self.run(" ".join([_X_ENV, "python3", f"{_FILES_DIR}/pace.py", *(shlex.quote(str(a)) for a in args)]))

    def _command(self, command: str, *, check: bool = True, **params: object) -> dict:
        response = self._send(
            "POST", f"{self.url}/port/{COMPUTER_SERVER_PORT}/cmd", json={"command": command, "params": params}
        )
        response.raise_for_status()
        # The computer server answers with one server-sent event: "data: {...}".
        result = json.loads(response.text.split("data: ", 1)[1])
        if check and not result.get("success"):
            raise DesktopError(f"{command} failed: {result.get('error')}")
        return result

    # -- browser ---------------------------------------------------------------

    def start_browser(self) -> None:
        """Start Chrome on the desktop if it isn't running, and open DevTools to the port route."""
        self.sandbox.fs.write(
            path=f"{_FILES_DIR}/cdp_forward.py",
            content=(Path(__file__).with_name("cdp_forward.py")).read_text(),
        )
        self.sandbox.fs.write(path=f"{_FILES_DIR}/start-chrome.sh", content=_START_CHROME)
        if not self._process_running("chrome"):
            self.sandbox.process.exec(
                command=f"bash {_FILES_DIR}/start-chrome.sh", name="chrome", timeout=timedelta(0)
            )
        # wait_for_ports only sees ports on the sandbox's own address, and
        # Chrome's DevTools listens on 127.0.0.1, so wait for it here.
        self.run(_WAIT_FOR_CHROME, timeout=timedelta(seconds=120))
        if not self._process_running("cdp-forward"):
            self.sandbox.process.exec(
                command=f"python3 {_FILES_DIR}/cdp_forward.py {FORWARDED_CDP_PORT} {CHROME_CDP_PORT}",
                name="cdp-forward",
                timeout=timedelta(0),
                restart_on_failure=True,
                max_restarts=-1,
                wait_for_ports=[FORWARDED_CDP_PORT],
            )

    def page(self) -> Page:
        """The browser tab in front, as a Playwright page on the desktop's Chrome."""
        if self._browser is None or not self._browser.is_connected():
            self._connect_browser()
        assert self._browser is not None
        context = self._browser.contexts[0]
        pages = [p for p in context.pages if not p.is_closed() and not p.url.startswith("devtools://")]
        visible = [p for p in pages if _is_visible(p)]  # The tab the user last switched to.
        page = (visible or pages)[-1] if pages else context.new_page()
        page.bring_to_front()
        self._show_browser(page)
        return page

    def _show_browser(self, page: Page) -> None:
        """Bring the page's window to the front, so the screen shows what the agent does.

        Another app's window can cover the browser, or the browser can be
        minimized; either way, mouse and keyboard input would go elsewhere.
        """
        windows = self._command("get_application_windows", app=f"{page.title()} - Google Chrome")["windows"]
        windows = windows or self._command("get_application_windows", app="Google Chrome")["windows"]
        if not windows or self._active_window() in windows:
            return
        session = page.context.new_cdp_session(page)
        try:
            window = session.send("Browser.getWindowForTarget")
            if window["bounds"].get("windowState") == "minimized":
                for state in ("normal", "maximized"):  # Chrome only maximizes from normal.
                    session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window["windowId"], "bounds": {"windowState": state}},
                    )
        finally:
            session.detach()
        # The computer server answers before the window manager acts, so its
        # success flag can be false; wait for the window instead.
        self._command("activate_window", check=False, window_id=windows[0])
        deadline = time.monotonic() + 3
        while self._active_window() not in windows and time.monotonic() < deadline:
            time.sleep(0.1)

    def _active_window(self) -> int:
        return self._command("get_current_window_id")["window_id"]

    def _disconnect_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()  # Over CDP this only disconnects; Chrome keeps running.
            except Exception:
                pass
            self._browser = None

    def _connect_browser(self) -> None:
        if self._playwright is None:
            self._playwright = _acquire_playwright()
        base = f"{self.url}/port/{FORWARDED_CDP_PORT}"
        try:
            version = self._send("GET", f"{base}/json/version")
            version.raise_for_status()
        except httpx.HTTPError as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
                raise
            self.start_browser()  # Chrome was closed; start it again.
            version = self._send("GET", f"{base}/json/version")
            version.raise_for_status()
        # Chrome reports ws://localhost/devtools/browser/<id>; reach it through the port route.
        path = "/devtools/" + version.json()["webSocketDebuggerUrl"].split("/devtools/", 1)[1]
        endpoint = base.replace("https://", "wss://", 1) + path
        self._browser = self._playwright.chromium.connect_over_cdp(endpoint, headers=self._auth())

    # -- helpers -----------------------------------------------------------------

    def run(self, command: str, timeout: timedelta = timedelta(seconds=60)) -> str:
        """Run a shell command in the sandbox as root and return its output.

        For your application's own checks and setup, not for the model: the
        agent works through the screen, keyboard, and browser like a person.
        The command line is kept in the sandbox's process records, so pass
        secrets in a file, not in ``command``.
        """
        result = self.sandbox.process.exec(command=command, wait_for_completion=True, timeout=timeout)
        if result.exit_code != 0:
            raise DesktopError(f"`{command}` exited {result.exit_code}: {result.stderr or result.stdout}")
        return result.stdout

    def _wait_for_desktop(self) -> None:
        self.run(_WAIT_FOR_DESKTOP, timeout=timedelta(seconds=90))

    def _process_running(self, name: str) -> bool:
        try:
            return self.sandbox.process.get(identifier=name).status == "running"
        except Exception:
            return False

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.get()}"}

    def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """A request to one of the sandbox's port routes, with one retry on a revoked token."""
        token = self._tokens.get()
        response = self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if response.status_code == 401:
            self._tokens.discard(token)
            response = self._http.request(method, url, headers=self._auth(), **kwargs)
        return response


# Playwright's sync API allows one instance per thread, so every Desktop in a
# thread shares it, and the last one to close stops it.
_thread_playwright = threading.local()


def _acquire_playwright() -> Playwright:
    state = _thread_playwright
    if getattr(state, "playwright", None) is None:
        state.playwright, state.users = sync_playwright().start(), 0
    state.users += 1
    return state.playwright


def _release_playwright() -> None:
    state = _thread_playwright
    state.users -= 1
    if state.users == 0:
        state.playwright.stop()
        state.playwright = None


def _is_visible(page: Page) -> bool:
    try:
        return page.evaluate("document.visibilityState") == "visible"
    except Exception:  # A page that is navigating or closing.
        return False


def _key_name(key: str, *, in_combo: bool) -> str:
    stripped = key.strip()
    if len(stripped) == 1:
        return stripped.lower() if in_combo else stripped
    normalized = stripped.lower().replace(" ", "").replace("-", "_")
    return _KEY_ALIASES.get(normalized.replace("_", ""), _KEY_ALIASES.get(normalized, normalized))
