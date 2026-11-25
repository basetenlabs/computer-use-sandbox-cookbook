"""A small website for the end-to-end tests, served inside the sandbox.

Pages:
* ``/``: a landing page with a link to sign up.
* ``/signup``: a form with a name, an email, a plan dropdown, and a terms
  checkbox. Each submission is appended to ``/tmp/test-site/signups.jsonl``.
* ``/login``: email sign-in with a one-time code that only "the user" knows
  (``481516``), to test handing a step to a person. A correct code is
  recorded in ``/tmp/test-site/logins.jsonl``.

Usage: python3 server.py <port>
"""

import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DATA_DIR = "/tmp/test-site"
ONE_TIME_CODE = "481516"

STYLE = """<style>
body { font-family: sans-serif; max-width: 560px; margin: 48px auto; padding: 0 16px; }
label { display: block; margin: 14px 0 4px; }
input, select { font-size: 16px; padding: 6px; width: 100%; box-sizing: border-box; }
input[type=checkbox] { width: auto; }
button, .button { margin-top: 18px; font-size: 16px; padding: 8px 16px; }
</style>"""


def page(title: str, body: str) -> bytes:
    return f"<!doctype html><html><head><title>{title}</title>{STYLE}</head><body>{body}</body></html>".encode()


LANDING = page(
    "Acme Cloud",
    "<h1>Acme Cloud</h1><p>Storage for teams.</p><a class=button href=/signup>Start free trial</a>",
)

SIGNUP = page(
    "Sign up - Acme Cloud",
    """<h1>Create your account</h1>
<form method=post action=/signup>
  <label for=name>Full name</label><input id=name name=name required>
  <label for=email>Work email</label><input id=email name=email type=email required>
  <label for=plan>Plan</label>
  <select id=plan name=plan><option>Starter</option><option>Pro</option><option>Team</option></select>
  <label><input type=checkbox name=terms value=yes required> I agree to the terms</label>
  <button type=submit>Create account</button>
</form>""",
)

LOGIN = page(
    "Sign in - Acme Cloud",
    """<h1>Sign in</h1>
<form method=post action=/login>
  <label for=email>Email</label><input id=email name=email type=email required>
  <button type=submit>Email me a code</button>
</form>""",
)


def verify_page(email: str, error: str = "") -> bytes:
    message = f"<p role=alert>{html.escape(error)}</p>" if error else ""
    return page(
        "Enter code - Acme Cloud",
        f"""<h1>Check your email</h1>
<p>We sent a 6-digit code to {html.escape(email)}.</p>{message}
<form method=post action=/verify>
  <input type=hidden name=email value="{html.escape(email)}">
  <label for=code>Code</label><input id=code name=code inputmode=numeric required>
  <button type=submit>Sign in</button>
</form>""",
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        pages = {"/": LANDING, "/signup": SIGNUP, "/login": LOGIN}
        self._send(pages.get(self.path.split("?")[0], page("Not found", "<h1>Not found</h1>")))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if self.path == "/signup":
            self._record("signups.jsonl", form)
            name, plan = html.escape(form.get("name", "")), html.escape(form.get("plan", ""))
            self._send(page("Welcome", f"<h1>Welcome, {name}!</h1><p>Your {plan} account is ready.</p>"))
        elif self.path == "/login":
            self._send(verify_page(form.get("email", "")))
        elif self.path == "/verify":
            if form.get("code", "").strip() == ONE_TIME_CODE:
                self._record("logins.jsonl", {"email": form.get("email", "")})
                self._send(page("Signed in", "<h1>You're signed in</h1>"))
            else:
                self._send(verify_page(form.get("email", ""), "That code is wrong. Try again."))
        else:
            self._send(page("Not found", "<h1>Not found</h1>"))

    def _record(self, filename: str, data: dict) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, filename), "a") as f:
            f.write(json.dumps(data) + "\n")

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
