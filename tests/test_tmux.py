import sys
import uuid

import pytest

from sessiondeck.models import DeckError
from sessiondeck.state import State
from sessiondeck.tmux import TmuxRuntime


@pytest.fixture
def runtime(tmp_path):
    runtime = TmuxRuntime(State(tmp_path / "state"), "deck-test-" + uuid.uuid4().hex,
                          [sys.executable, "-c", "import time; time.sleep(300)"])
    runtime.ensure()
    try:
        yield runtime
    finally:
        runtime.command("kill-server", check=False)


def test_generation_and_stable_ids(runtime, tmp_path):
    identity = runtime.ensure()
    pane = runtime.new_window(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(300)"])
    runtime.command("rename-window", "-t", pane.window_id, "renamed")
    assert runtime.inspect(pane.pane_id, identity.generation) == pane
    runtime.focus(pane)
    runtime.show_dashboard()
    assert runtime.ensure() == identity
    with pytest.raises(DeckError, match="earlier"):
        runtime.inspect(pane.pane_id, "wrong-generation")


def test_paths_are_not_shell_code(runtime, tmp_path):
    directory = tmp_path / "space проект; $(touch UNEXPECTED)"
    directory.mkdir()
    pane = runtime.new_window(str(directory), [sys.executable, "-c", "import os,time; print(os.getcwd(),flush=True); time.sleep(300)"])
    output = runtime.command("display-message", "-p", "-t", pane.pane_id, "#{pane_current_path}").stdout.strip()
    assert output == str(directory)
    assert not (tmp_path / "UNEXPECTED").exists()


def test_unified_detach_binding(runtime):
    identity = runtime.ensure()
    binding = runtime.command("list-keys", "-T", "prefix", "d").stdout
    assert "detach-client" in binding
    assert "select-window" in binding
    assert identity.dashboard_pane_id in binding
    assert "C-q" in runtime.command("list-keys", "-T", "root").stdout


def test_missing_pane(runtime, tmp_path):
    pane = runtime.new_window(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(300)"])
    runtime.command("kill-pane", "-t", pane.pane_id)
    with pytest.raises(DeckError, match="no longer exists"):
        runtime.inspect(pane.pane_id, pane.generation)


def test_close_agent_binding_rejects_dashboard_and_closes_agent(runtime, tmp_path):
    dashboard = runtime.ensure()
    with pytest.raises(DeckError, match="dashboard"):
        runtime.close_agent_binding(runtime.inspect(dashboard.dashboard_pane_id, dashboard.generation))
    agent = runtime.new_window(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(300)"])
    runtime.close_agent_binding(agent)
    with pytest.raises(DeckError, match="no longer exists"):
        runtime.inspect(agent.pane_id, agent.generation)
