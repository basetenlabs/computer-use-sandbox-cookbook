"""The desktop itself, without a model: screen, input, browser, live view, and access."""

import io
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image
from websockets.sync.client import connect

from computer_use import Desktop
from conftest import SITE


def test_screenshot_shows_the_desktop(desktop: Desktop) -> None:
    image = Image.open(io.BytesIO(desktop.screenshot()))
    assert image.size == (1024, 768)
    assert len(image.convert("RGB").getcolors(maxcolors=1 << 24)) > 50  # Drawn, not a blank frame.


def test_clicks_and_typing_land_where_the_screenshot_shows(desktop: Desktop) -> None:
    page = desktop.page()
    page.goto(f"{SITE}/signup")
    box = page.locator("#name").bounding_box()
    # Where the page's viewport sits on the screen.
    origin = page.evaluate(
        "() => ({x: window.screenX + window.outerWidth - window.innerWidth,"
        " y: window.screenY + window.outerHeight - window.innerHeight})"
    )
    desktop.click(round(origin["x"] + box["x"] + box["width"] / 2), round(origin["y"] + box["y"] + box["height"] / 2))
    desktop.type_text("Grace Hopper")
    assert page.input_value("#name") == "Grace Hopper"


def test_the_browser_comes_to_the_front(desktop: Desktop) -> None:
    def front_window_is_the_browser() -> bool:
        current = desktop._command("get_current_window_id")["window_id"]
        return current in desktop._command("get_application_windows", app="Google Chrome")["windows"]

    # Cover the browser with a terminal, as an agent might.
    desktop.run("su cua -c 'DISPLAY=:1 setsid xfce4-terminal --title=cover >/dev/null 2>&1 &'")
    desktop.run("for i in $(seq 1 40); do pgrep -f 'title=cover' >/dev/null && sleep 1 && exit 0; sleep 0.25; done; exit 1")
    cover = desktop._command("get_application_windows", app="cover")["windows"][0]
    desktop._command("activate_window", check=False, window_id=cover)  # Its success flag comes too early.
    for _ in range(30):
        if desktop._command("get_current_window_id")["window_id"] == cover:
            break
        time.sleep(0.1)
    assert not front_window_is_the_browser()
    desktop.page()
    assert front_window_is_the_browser()

    # Minimize the browser.
    page = desktop.page()
    session = page.context.new_cdp_session(page)
    window_id = session.send("Browser.getWindowForTarget")["windowId"]
    session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "minimized"}})
    desktop.page()
    assert session.send("Browser.getWindowBounds", {"windowId": window_id})["bounds"]["windowState"] != "minimized"
    assert front_window_is_the_browser()
    session.detach()
    desktop.run("pkill -f '[t]itle=cover'; true")


def test_key_combinations_drive_the_browser(desktop: Desktop) -> None:
    page = desktop.page()
    page.goto("about:blank")
    desktop.press("ctrl+l")
    desktop.type_text(f"{SITE}/login")
    desktop.press("Return")
    page.wait_for_url(f"{SITE}/login", timeout=15_000)


def test_live_view_streams_the_desktop(desktop: Desktop) -> None:
    url = desktop.live_view_url(expires_in=timedelta(minutes=10))
    response = httpx.get(url, timeout=30)
    assert response.status_code == 200 and "noVNC" in response.text

    parsed = urlparse(url)
    token = parse_qs(parsed.query)["preview_token"][0]

    endpoint = f"wss://{parsed.netloc}/websockify?preview_token={token}"
    with connect(endpoint, subprotocols=["binary"], open_timeout=30) as socket:
        assert socket.recv(timeout=30).startswith(b"RFB ")  # The VNC handshake.


def test_live_view_needs_its_token(desktop: Desktop) -> None:
    url = desktop.live_view_url(expires_in=timedelta(minutes=10)).split("?")[0]
    response = httpx.get(f"{url}/vnc.html", timeout=30)
    assert response.status_code in (401, 403)


def test_desktop_and_browser_routes_need_a_sandbox_token(desktop: Desktop) -> None:
    screenshot = httpx.post(f"{desktop.url}/port/8000/cmd", json={"command": "screenshot"}, timeout=30)
    devtools = httpx.get(f"{desktop.url}/port/9223/json/version", timeout=30)
    assert screenshot.status_code == 401
    assert devtools.status_code == 401


def test_the_desktop_user_has_no_root(desktop: Desktop) -> None:
    # Whatever runs as the desktop's user (an app, a terminal the agent opens) stays that user.
    assert desktop.run("su cua -c 'sudo -n true' > /dev/null 2>&1; echo $?").strip() != "0"


def test_web_pages_cannot_reach_the_computer_server(desktop: Desktop) -> None:
    # The computer server inside the sandbox answers any origin; Chrome's policy keeps pages away from it.
    page = desktop.page()
    page.goto("https://example.com", wait_until="domcontentloaded")
    outcome = page.evaluate(
        """() => Promise.race([
            fetch("http://localhost:8000/cmd", {method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({command: "get_current_window_id", params: {}})})
              .then(() => "reached", () => "blocked"),
            new Promise(resolve => setTimeout(() => resolve("no answer"), 8000)),
        ])"""
    )
    page.goto("about:blank")
    assert outcome == "blocked"


def test_reconnecting_keeps_the_browser_session(desktop: Desktop) -> None:
    page = desktop.page()
    page.goto(f"{SITE}/signup")
    page.fill("#name", "Still here")
    with Desktop.open(desktop.name) as again:  # A second connection, like another backend process.
        other = again.page()
        assert other.url == f"{SITE}/signup"
        assert other.input_value("#name") == "Still here"
