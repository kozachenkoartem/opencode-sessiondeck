import sys
import uuid

import pytest

from sessiondeck.models import DeckError, Registration
from sessiondeck.registration import register_session
from sessiondeck.registry import Registry
from sessiondeck.state import State
from sessiondeck.tmux import TmuxRuntime

pytestmark = pytest.mark.live
IDLE_PANE = [sys.executable, "-c", "import time; time.sleep(300)"]


@pytest.mark.parametrize("old_pane", ["live", "missing", "old-generation"])
async def test_recycled_endpoint_only_releases_confirmed_obsolete_binding(live_server, tmp_path, old_pane):
    client, _, env, directory = live_server
    registry = Registry(State(tmp_path / "deck-state"))
    runtime = TmuxRuntime(registry.state, "deck-binding-" + uuid.uuid4().hex, IDLE_PANE)
    credential = registry.state.save_credential("opencode", env["OPENCODE_SERVER_PASSWORD"])

    async def register(pane, conversation):
        registration = Registration("work", directory, client.endpoint, conversation.id,
                                    pane.socket_path, pane.generation, pane.pane_id, credential_ref=credential)
        return await register_session(registry, runtime, registration)

    first = await client.create_session(directory, "Old binding")
    second = await client.create_session(directory, "Replacement binding")
    try:
        old = runtime.new_window(directory, IDLE_PANE)
        original = await register(old, first)
        if old_pane == "missing":
            runtime.command("kill-pane", "-t", old.pane_id)
        elif old_pane == "old-generation":
            runtime.command("kill-server")
        new = runtime.new_window(directory, IDLE_PANE)
        if old_pane == "live":
            with pytest.raises(DeckError) as error:
                await register(new, second)
            assert error.value.code == "binding_conflict"
            assert runtime.inspect(old.pane_id, old.generation) == old
            assert len(registry.list()) == 1
        else:
            replacement = await register(new, second)
            assert replacement.deck_id != original.deck_id
            assert not registry.get(original.deck_id).retired
            assert len(registry.list()) == 2
            assert runtime.inspect(new.pane_id, new.generation) == new
    finally:
        runtime.command("kill-server", check=False)
        for conversation in (first, second):
            await client.request("DELETE", f"/session/{conversation.id}", directory=directory)
