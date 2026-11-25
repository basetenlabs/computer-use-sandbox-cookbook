"""Run one task on a cloud desktop from the command line.

    computer-use "Find the cheapest flight from SFO to JFK next Friday"
    computer-use --delete              # delete your desktop

Prints a live view link to watch (or take over), each step the agent takes,
and its answer. When the agent needs you, it asks in the terminal.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re

from baseten.sandbox import SandboxClient

from computer_use.agent import DEFAULT_MODEL, Step, run_task
from computer_use.desktop import IMAGE, Desktop


def main() -> None:
    parser = argparse.ArgumentParser(prog="computer-use", description=__doc__.split("\n\n")[0])
    parser.add_argument("task", nargs="?", help="what the agent should do")
    parser.add_argument(
        "--desktop",
        default="desktop-" + re.sub(r"[^a-z0-9-]+", "-", getpass.getuser().lower()),
        help="sandbox name; reusing a name reconnects to that desktop (default: desktop-<your user>)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Baseten Model APIs model (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-browser-tools", action="store_true", help="use only the screen, mouse, and keyboard")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument(
        "--human-pace", action="store_true", help="glide the pointer and type key by key, for people watching"
    )
    parser.add_argument("--image", default=IMAGE, help=f"sandbox image (default: {IMAGE}, built by computer-use-image)")
    parser.add_argument(
        "--delete", action="store_true", help="delete the desktop when the task ends, or now if there is no task"
    )
    args = parser.parse_args()

    if args.task is None:
        if not args.delete:
            parser.error("give a task, or --delete to delete the desktop")
        client = SandboxClient(api_key=os.environ["BASETEN_API_KEY"])
        try:
            client.delete(name=args.desktop)
        finally:
            client.close()
        print(f"Deleted {args.desktop}.")
        return

    print(f"Opening desktop {args.desktop} (about a minute the first time, seconds after)...")
    with Desktop.open(args.desktop, image=args.image, human_pace=args.human_pace) as desktop:
        print(f"\nWatch or take over: {desktop.live_view_url()}\n")

        def show(step: Step) -> None:
            arguments = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items())
            print(f"{step.number:>3}. {step.tool}({arguments[:160]})")

        def ask_user(question: str) -> str:
            return input(f"\nThe agent asks: {question}\nYour answer: ")

        try:
            result = run_task(
                desktop,
                args.task,
                model=args.model,
                browser_tools=not args.no_browser_tools,
                max_turns=args.max_turns,
                on_step=show,
                ask_user=ask_user,
            )
            print(f"\n{result.answer}")
            print(
                f"({len(result.steps)} step{'s' if len(result.steps) != 1 else ''}, {result.seconds:.0f}s, "
                f"{result.input_tokens:,} input and {result.output_tokens:,} output tokens)"
            )
        finally:
            if args.delete:
                desktop.delete()
                print(f"Deleted {args.desktop}.")
            else:
                print(f"The desktop stays up for reuse. Delete it with: computer-use --delete --desktop {args.desktop}")


if __name__ == "__main__":
    main()
