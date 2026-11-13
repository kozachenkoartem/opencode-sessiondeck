import fcntl
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from .models import DeckError, PaneBinding, RuntimeIdentity
from .state import State

DEFAULT_SOCKET = "opencode-sessiondeck"


class TmuxRuntime:
    def __init__(self, state: State, socket_name: str | None = None, dashboard_argv=None):
        self.state = state
        self.socket_name = socket_name or os.environ.get("SESSIONDECK_TMUX_SOCKET", DEFAULT_SOCKET)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.socket_name):
            raise DeckError("invalid_socket", "Invalid SessionDeck socket name", 2)
        self.dashboard_argv = dashboard_argv or [sys.executable, "-m", "sessiondeck", "_dashboard"]

    def command(self, *args, check=True, capture=True):
        env = os.environ.copy()
        env.pop("TMUX", None)
        env["SESSIONDECK_STATE_DIR"] = str(self.state.root)
        env["SESSIONDECK_TMUX_SOCKET"] = self.socket_name
        try:
            result = subprocess.run(["tmux", "-L", self.socket_name, "-f", "/dev/null", *args],
                                    env=env, text=True, capture_output=capture, timeout=10 if capture else None)
        except (OSError, subprocess.TimeoutExpired):
            raise DeckError("tmux_unavailable", "tmux command could not complete") from None
        if check and result.returncode:
            raise DeckError("tmux_error", "tmux operation failed: " + (result.stderr or "").strip())
        return result

    def option(self, name: str) -> str:
        result = self.command("show-option", "-s", "-v", name, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def ensure(self) -> RuntimeIdentity:
        with (self.state.root / "runtime.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            exists = self.command("has-session", "-t", "=main", check=False).returncode == 0
            if not exists:
                pane = self.command("new-session", "-d", "-s", "main", "-n", "dashboard", "-P", "-F", "#{pane_id}",
                                    "-e", f"SESSIONDECK_STATE_DIR={self.state.root}",
                                    "-e", f"SESSIONDECK_TMUX_SOCKET={self.socket_name}", *self.dashboard_argv).stdout.strip()
                self.command("set-option", "-s", "@sessiondeck_generation", str(uuid.uuid4()))
                self.command("set-option", "-s", "@sessiondeck_dashboard", pane)
            elif not self.option("@sessiondeck_generation"):
                raise DeckError("unmanaged_runtime", "Existing tmux runtime is not owned by SessionDeck")
            dashboard = self.option("@sessiondeck_dashboard")
            panes = self.panes()
            if dashboard not in panes or panes[dashboard].dead:
                dashboard = self.command("new-window", "-d", "-t", "main:", "-n", "dashboard", "-P", "-F", "#{pane_id}",
                                         *self.dashboard_argv).stdout.strip()
                self.command("set-option", "-s", "@sessiondeck_dashboard", dashboard)
                panes = self.panes()
            self.command("bind-key", "d", "if-shell", "-F", f"#{{==:#{{pane_id}},{dashboard}}}",
                         "detach-client", f"select-window -t {panes[dashboard].window_id}")
            self.command("bind-key", "-n", "C-q", "detach-client")
            self.command("bind-key", "-n", "C-Space", "if-shell", "-F", f"#{{==:#{{pane_id}},{dashboard}}}",
                         "detach-client", f"select-window -t {panes[dashboard].window_id}")
            self.command("set-option", "-g", "status-right", "Ctrl-Space: back / detach")
            pane = self.panes()[dashboard]
            return RuntimeIdentity(pane.socket_path, pane.generation, pane.session_id, dashboard)

    def panes(self) -> dict[str, PaneBinding]:
        generation = self.option("@sessiondeck_generation")
        output = self.command("list-panes", "-a", "-F", "#{socket_path}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_dead}").stdout
        values = {}
        for line in output.splitlines():
            socket, session, window, pane, dead = line.split("\t")
            values[pane] = PaneBinding(str(Path(socket).resolve()), generation, session, window, pane, dead == "1")
        return values

    def inspect(self, pane_id: str, expected_generation: str) -> PaneBinding:
        if not re.fullmatch(r"%\d+", pane_id):
            raise DeckError("invalid_pane", "Expected a stable tmux pane ID", 2)
        actual = self.option("@sessiondeck_generation")
        if not actual:
            raise DeckError("runtime_missing", "SessionDeck tmux runtime is not running")
        if actual != expected_generation:
            raise DeckError("stale_runtime", "This registration belongs to an earlier tmux server", 4)
        pane = self.panes().get(pane_id)
        if not pane:
            raise DeckError("pane_missing", "Registered tmux pane no longer exists")
        return pane

    def new_window(self, directory: str, argv: list[str]) -> PaneBinding:
        identity = self.ensure()
        pane_id = self.command("new-window", "-d", "-t", "main:", "-n", "agent", "-c", directory,
                               "-P", "-F", "#{pane_id}", *argv).stdout.strip()
        return self.inspect(pane_id, identity.generation)

    def focus(self, binding: PaneBinding):
        current = self.inspect(binding.pane_id, binding.generation)
        if current.socket_path != binding.socket_path or current.dead:
            raise DeckError("binding_mismatch", "Registered pane is no longer attachable", 4)
        self.command("select-window", "-t", current.window_id)
        self.command("select-pane", "-t", current.pane_id)

    def close_agent_binding(self, binding: PaneBinding):
        current = self.inspect(binding.pane_id, binding.generation)
        dashboard = self.option("@sessiondeck_dashboard")
        if current.pane_id == dashboard:
            raise DeckError("invalid_binding", "Cannot close the dashboard pane", 4)
        if current.socket_path != binding.socket_path or current.window_id != binding.window_id:
            raise DeckError("binding_mismatch", "Agent pane no longer owns its registered window", 4)
        self.command("kill-window", "-t", current.window_id)

    def show_dashboard(self):
        identity = self.ensure()
        self.focus(self.inspect(identity.dashboard_pane_id, identity.generation))

    def attach(self):
        identity = self.ensure()
        current_socket = os.environ.get("TMUX", "").split(",")[0]
        if current_socket and str(Path(current_socket).resolve()) != identity.socket_path:
            raise DeckError("nested_tmux", "Run sessiondeck outside the other tmux server")
        self.show_dashboard()
        if not current_socket:
            self.command("attach-session", "-t", "=main", capture=False)
