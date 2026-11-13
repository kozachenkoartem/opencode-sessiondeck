import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import asdict

from . import __version__
from .models import DeckError
from .registration import register_input
from .registry import Registry
from .state import State
from .tmux import TmuxRuntime


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DeckError("invalid_input", message, 2)


def parser():
    root = Parser(prog="sessiondeck")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    api = commands.add_parser("api").add_subparsers(dest="api_version", required=True)
    v1 = api.add_parser("v1").add_subparsers(dest="action", required=True)
    for action in ("runtime", "register"):
        v1.add_parser(action)
    listing = v1.add_parser("list")
    listing.add_argument("--workspace", choices=["work", "personal", "all"], default="all")
    listing.add_argument("--directory")
    listing.add_argument("--include-archived", action="store_true")
    for action in ("archive", "unarchive", "unregister"):
        v1.add_parser(action).add_argument("deck_id")
    commands.add_parser("_dashboard")
    commands.add_parser("_run").add_argument("launch_id")
    commands.add_parser("_backend").add_argument("parent_pid", type=int)
    start = commands.add_parser("start")
    start.add_argument("--workspace", choices=["work", "personal"], default="work")
    start.add_argument("--directory", help="Project directory; inferred from --session when omitted")
    start.add_argument("--session", help="Existing OpenCode conversation ID to resume")
    start.add_argument("--title")
    start.add_argument("--reference-url")
    return root


def main():
    api_mode = len(sys.argv) > 1 and sys.argv[1] == "api"
    try:
        args = parser().parse_args()
        if args.command == "start" and not (args.directory or args.session):
            raise DeckError("invalid_input", "Provide --directory for a new session, or --session to resume", 2)
        if args.command == "_backend":
            from .backend_exec import exec_backend
            exec_backend(args.parent_pid)
            return
        registry = Registry(State())
        runtime = TmuxRuntime(registry.state)
        if args.command == "api":
            if args.action == "runtime":
                data = asdict(runtime.ensure())
            elif args.action == "register":
                value = json.load(sys.stdin)
                if not isinstance(value, dict):
                    raise DeckError("invalid_input", "Registration must be a JSON object", 2)
                data = asdict(asyncio.run(register_input(registry, runtime, value)))
            elif args.action == "list":
                data = [asdict(item) for item in registry.list(args.workspace, args.directory, args.include_archived)]
            else:
                if args.action == "unregister":
                    registry.unregister(args.deck_id)
                else:
                    registry.set_archived(args.deck_id, args.action == "archive")
                data = {"deck_id": args.deck_id}
            print(json.dumps({"api_version": 1, "ok": True, "data": data}))
        elif args.command == "_dashboard":
            from .ui.app import run_dashboard
            run_dashboard(registry, runtime)
        elif args.command == "_run":
            from .runner import run_attempt
            raise SystemExit(run_attempt(registry, runtime, args.launch_id))
        elif args.command == "start":
            from .runner import start_session
            result = start_session(registry, runtime, args.workspace, args.directory, args.session, args.title, args.reference_url)
            print(json.dumps({key: value for key, value in result.items() if key in {"id", "state", "deck_id", "error"}}))
        else:
            registry.request_recovery()
            runtime.attach()
    except (DeckError, ValueError, OSError, sqlite3.Error) as exc:
        if not isinstance(exc, DeckError):
            exc = (DeckError("invalid_input", "Invalid JSON input", 2) if isinstance(exc, ValueError)
                   else DeckError("state_error", "Cannot access local SessionDeck state"))
        if api_mode:
            print(json.dumps({"api_version": 1, "ok": False, "error": {"code": exc.code, "message": str(exc)}}))
        else:
            print(f"sessiondeck: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code) from None
