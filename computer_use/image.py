"""Build the desktop image the agent uses, once per team.

    computer-use-image                 # builds computer-use-desktop:latest

It starts from the same desktop as Baseten's built-in ``baseten/cua-xfce``
image (Ubuntu 22.04, XFCE, VNC, noVNC and the computer server) and adds:

* Google Chrome, so a new desktop doesn't download it on its first start.
* Mousepad, a text editor, so the desktop has the everyday apps a person
  expects: a browser, a file manager, a terminal and an editor.
* xdotool, for ``human_pace``: the pointer glides and text is typed key by
  key, so people watching the live view can follow along.
* Chrome policies that turn off sign-in, password saving, translation and
  other prompts that would cover the page the agent is working on, and that
  stop web pages from reaching services on the sandbox itself. The computer
  server answers requests from any web origin, so without the policy a page
  could send it commands through the browser.
* No root for the desktop user. The base image gives ``cua`` passwordless
  ``sudo``; this image takes it away, so anything running as the desktop user
  (an app, a terminal the agent opens, a web page that gets a foothold) stays
  that user.

The built-in image is missing its icons and application menu, which makes
the desktop harder for a model (and a person) to find their way around.
This image keeps them.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timedelta

from baseten.sandbox import ImageBuilder, SandboxClient

DEFAULT_IMAGE_NAME = "computer-use-desktop"
# Pinned: the code relies on the computer server's commands and responses in
# this build. Move the pin deliberately and rerun the tests.
BASE_IMAGE = "ghcr.io/blaxel-ai/sandbox-cua-xfce@sha256:f9c9173bfb36c39f271ca43a2ec3431b7eee9e775fbe06025eab0ad390039262"
CHROME_DEB = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"

CHROME_POLICIES = {
    "BrowserSignin": 0,
    "SyncDisabled": True,
    "PasswordManagerEnabled": False,
    "AutofillAddressEnabled": False,
    "AutofillCreditCardEnabled": False,
    "TranslateEnabled": False,
    "PromotionsEnabled": False,
    "PrivacySandboxPromptEnabled": False,
    "DefaultBrowserSettingEnabled": False,
    "MetricsReportingEnabled": False,
    # Web pages can't reach the sandbox's own services, such as the computer
    # server on localhost:8000, or ask the user to allow it.
    "LocalNetworkAccessBlockedForUrls": ["*"],
}


def image_builder() -> ImageBuilder:
    """The desktop image as a Dockerfile, starting from the built-in desktop."""
    return (
        ImageBuilder.from_registry(BASE_IMAGE)
        .user("root")
        .run_commands(
            "apt-get update"
            f" && wget -q -O /tmp/chrome.deb {CHROME_DEB}"
            " && apt-get install -y --no-install-recommends /tmp/chrome.deb mousepad xdotool"
            " && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*",
            # Take away the desktop user's root: no sudo group, no NOPASSWD rule, no password.
            "gpasswd -d cua sudo && sed -i '/^cua ALL/d' /etc/sudoers && passwd -l cua",
        )
        .add_file("/etc/opt/chrome/policies/managed/computer-use.json", json.dumps(CHROME_POLICIES, indent=2) + "\n")
        # The base image starts its desktop services with supervisord from
        # this entrypoint, and supervisord starts the sandbox API.
        .entrypoint("/entrypoint.sh")
    )


def build_image(name: str = DEFAULT_IMAGE_NAME, *, api_key: str | None = None) -> str:
    """Build the image into your team's images and return its reference."""
    client = SandboxClient(api_key=api_key or os.environ["BASETEN_API_KEY"])
    try:
        client.images.push(name=name, builder=image_builder(), timeout=timedelta(minutes=30))
    finally:
        client.close()
    return f"{name}:latest"


def main() -> None:
    parser = argparse.ArgumentParser(prog="computer-use-image", description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", default=DEFAULT_IMAGE_NAME, help=f"image name (default: {DEFAULT_IMAGE_NAME})")
    args = parser.parse_args()
    print(f"Building {args.name} (about 2 minutes the first time)...")
    started = time.monotonic()
    reference = build_image(args.name)
    print(f"Built {reference} in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    main()
