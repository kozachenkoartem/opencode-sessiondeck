import asyncio

import pytest

from sessiondeck.models import Endpoint
from sessiondeck.monitor import Monitor
from sessiondeck.opencode import OpenCodeClient
from sessiondeck.runner import start_session
from sessiondeck.ui.app import SessionDeckApp
from tests.integration.test_runner_live import native_runtime, running, until

pytestmark = pytest.mark.live


@pytest.mark.parametrize("decision,key", [("once", "a"), ("reject", "d")])
async def test_real_pending_permission_from_dashboard(native_runtime, tmp_path, decision, key):
    registry, runtime = native_runtime
    previous = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path))
    old = registry.get(previous["deck_id"])
    runtime.command("kill-pane", "-t", old.binding.pane_id)
    await until(lambda: not running(previous["backend_pid"]))
    attempt = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path),
                                      session_id=old.registration.opencode_session_id)
    session = registry.get(attempt["deck_id"])
    async with OpenCodeClient(Endpoint(session.registration.endpoint),
                               auth=registry.state.auth(session.registration.credential_ref)) as client:
        response = await client.http.post(f"/session/{session.registration.opencode_session_id}/prompt_async",
                                          params={"directory": session.api_directory}, json={
            "agent": "build", "model": {"providerID": "sessiondeck-fixture", "modelID": "fixture"},
            "parts": [{"type": "text", "text": "Run the local directory probe"}],
        })
        assert response.status_code == 204
        monitor = Monitor(registry, runtime)
        app = SessionDeckApp(registry, monitor, runtime)
        async with app.run_test() as pilot:
            await until(lambda: bool(app.snapshot.sessions.get(session.deck_id) and
                                    app.snapshot.sessions[session.deck_id].permissions))
            await pilot.press("enter")
            assert app.selected_ref is not None
            await pilot.press(key)
            def confirmed():
                with registry.connect() as db:
                    row = db.execute("SELECT outcome FROM audit").fetchone()
                    return row is not None and row[0] == "confirmed"
            await until(confirmed)
            with registry.connect() as db:
                audit = db.execute("SELECT decision,outcome FROM audit").fetchone()
                assert tuple(audit) == (decision, "confirmed")
            await until(lambda: bool(app.snapshot.sessions.get(old.deck_id) and
                                    app.snapshot.sessions[old.deck_id].status == "stopped"))
            await pilot.press("c")
            await until(lambda: registry.get(old.deck_id).archived)
            assert not registry.get(session.deck_id).archived
            assert running(attempt["backend_pid"])
        async with asyncio.timeout(20):
            while (await client.snapshot(session.api_directory, session.registration.opencode_session_id)).execution != "idle":
                await asyncio.sleep(.1)
        messages = await client.request("GET", f"/session/{session.registration.opencode_session_id}/message", directory=session.api_directory)
        tools = [part for message in messages for part in message["parts"] if part["type"] == "tool" and part["tool"] == "bash"]
        assert tools
        assert tools[-1]["state"]["status"] == ("completed" if decision == "once" else "error")
