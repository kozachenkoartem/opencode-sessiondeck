import os
import pty
import subprocess
import sys
import time
import uuid

from sessiondeck.state import State
from sessiondeck.tmux import TmuxRuntime


def test_unified_return_then_detach_keeps_agents(tmp_path):
    runtime = TmuxRuntime(State(tmp_path / "state"), "deck-nav-" + uuid.uuid4().hex,
                          [sys.executable, "-c", "import time; time.sleep(300)"])
    identity = runtime.ensure()
    agent = runtime.new_window(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(300)"])
    runtime.focus(agent)
    master, slave = pty.openpty()
    env = os.environ.copy()
    env.pop("TMUX", None)
    env["TERM"] = "xterm-256color"
    client = subprocess.Popen(["tmux", "-L", runtime.socket_name, "attach-session", "-t", "main"],
                               env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)

    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.read(master, 65536)
            except (BlockingIOError, OSError):
                pass
            if predicate():
                return
            time.sleep(.05)
        raise AssertionError("tmux navigation timed out")

    try:
        wait_for(lambda: runtime.command("list-clients", "-F", "#{client_pid}").stdout.strip())
        os.write(master, b"\x02d")
        wait_for(lambda: runtime.command("display-message", "-p", "-t", "main:", "#{pane_id}").stdout.strip() == identity.dashboard_pane_id)
        assert runtime.inspect(agent.pane_id, agent.generation) == agent
        os.write(master, b"\x02d")
        wait_for(lambda: not runtime.command("list-clients", "-F", "#{client_pid}").stdout.strip())
        assert client.wait(timeout=5) == 0
        assert runtime.inspect(agent.pane_id, agent.generation) == agent
    finally:
        if client.poll() is None:
            client.kill()
        client.wait()
        os.close(master)
        runtime.command("kill-server", check=False)
