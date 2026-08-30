"""Command line entry point: `python -m bridge <command>`.

There are four commands, and only the first two are meant for a person:

* `setup` — the whole bootstrap, ending in a link to the guardian bot;
* `run`   — the service, started by systemd rather than by hand;
* `validate-config` and `telegram-login` — repair tools, for the two cases the
  bot cannot fix itself.

Connecting MAX, choosing dialogs, checking status and restarting all happen in
the guardian chat. That is why there is no `max-login` here any more: a command
that asks for an SMS code in a terminal is a command that requires an SSH
session to stay open, which is exactly what this design removed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from bridge.cli import cutover as cutover_cli
from bridge.cli import healthcheck, owner_session, telegram_login, validate
from bridge.cli import run as run_cli
from bridge.cli import setup as setup_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bridge",
        description="Personal MAX <-> Telegram bridge.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="path to the YAML config (default: $BRIDGE_CONFIG)",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    setup_command = commands.add_parser(
        "setup", help="set everything up and hand over to Telegram"
    )
    setup_command.add_argument(
        "--instance",
        default=None,
        help=(
            "name a second installation on this host, so its systemd unit is "
            "telemax-<name>.service instead of overwriting the first one's"
        ),
    )
    setup_command.add_argument(
        "--runtime",
        choices=("systemd", "docker"),
        default="systemd",
        help=argparse.SUPPRESS,
    )
    setup_command.add_argument(
        "--with-session",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    setup_command.add_argument(
        "--manual-guardian",
        action="store_true",
        help=(
            "recovery path: paste an existing Guardian token instead of scanning "
            "Telegram QR and creating the bot automatically"
        ),
    )
    setup_command.add_argument(
        "--adopt-guardian",
        action="store_true",
        help=(
            "take the guardian bot you already have, at whatever username it "
            "has, instead of deriving one from your Telegram and MAX accounts. "
            "The derived name is what lets a clean machine with the same two "
            "accounts find the same bots, so this is the legacy path"
        ),
    )
    commands.add_parser("run", help="start the service (systemd does this for you)")
    commands.add_parser("healthcheck", help="check the local supervisor heartbeat")
    commands.add_parser("validate-config", help="check the configuration and print what it means")
    commands.add_parser(
        "telegram-login",
        help="re-open the Telegram user session if it ever expires",
    )
    cutover_command = commands.add_parser(
        "cutover-v2",
        help=(
            "one-time move to V2 naming: destroy the legacy bots' local state "
            "and keep the accounts. Read-only without --yes-destroy"
        ),
    )
    cutover_command.add_argument(
        "--yes-destroy",
        action="store_true",
        help="the first owner gate: empty the bridge-scoped tables and forget the tokens",
    )
    commands.add_parser(
        "telegram-sync",
        help="link the owner's Telegram by QR so Telemax sees owner-side edits/deletes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "setup":
        return setup_cli.run(
            args.config,
            instance=args.instance,
            use_session=not args.manual_guardian,
            adopt=args.adopt_guardian,
            deployment=args.runtime,
        )
    if args.command == "validate-config":
        return validate.run(args.config)
    if args.command == "telegram-login":
        return telegram_login.run(args.config)
    if args.command == "cutover-v2":
        return cutover_cli.run(args.config, destroy=args.yes_destroy)
    if args.command == "telegram-sync":
        return owner_session.run(args.config)
    if args.command == "run":
        return run_cli.run(args.config)
    if args.command == "healthcheck":
        return healthcheck.run(args.config)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
