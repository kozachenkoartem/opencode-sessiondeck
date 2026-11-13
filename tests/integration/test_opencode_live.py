import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from sessiondeck.models import DeckError, Endpoint
from sessiondeck.opencode import OpenCodeClient

pytestmark = pytest.mark.live
TEST_PROJECT_ID = "0123456789abcdef0123456789abcdef01234567"


async def prepare_git_project(directory):
    git = await asyncio.create_subprocess_exec("git", "init", "--quiet", directory,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await git.communicate()
    assert git.returncode == 0
    # Задаём идентификатор тестового проекта без создания коммитов.
    (Path(directory) / ".git" / "opencode").write_text(TEST_PROJECT_ID)


async def test_session_list_is_not_a_global_directory_resolver(live_server, tmp_path):
    client, _, env, directory = live_server
    await prepare_git_project(directory)
    session = await client.create_session(directory, "DIRECTORY_LOOKUP_PROBE")
    elsewhere = tmp_path / "unrelated directory"
    elsewhere.mkdir()
    try:
        process = await asyncio.create_subprocess_exec("opencode", "session", "list", "--format", "json",
                                                       env=env, cwd=elsewhere, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0
        rows = json.loads(stdout) if stdout.strip() else []
        row = next((row for row in rows if row["id"] == session.id), None)
        assert row is None, "The pinned CLI's list is scoped to the caller's Git project"
    finally:
        await client.request("DELETE", f"/session/{session.id}", directory=directory)


@pytest.mark.parametrize("newer_count", [0, 101], ids=["other-project", "past-default-limit"])
async def test_cli_resume_by_id_infers_directory(live_server, tmp_path, newer_count):
    from sessiondeck.registry import Registry
    from sessiondeck.state import State
    from sessiondeck.tmux import TmuxRuntime

    client, original_server, env, directory = live_server
    await prepare_git_project(directory)
    session = await client.create_session(directory, "AUTOMATIC_DIRECTORY_RESUME")
    newer = [await client.create_session(directory, "Newer metadata fixture") for _ in range(newer_count)]
    if newer:
        listed = await client.request("GET", "/session", directory=directory)
        assert len(listed) == 100 and session.id not in {item["id"] for item in listed}
    elsewhere = tmp_path / "different caller cwd"
    elsewhere.mkdir()
    state = State(tmp_path / "deck-state")
    socket = "deck-auto-dir-" + uuid.uuid4().hex
    env = env | {"SESSIONDECK_STATE_DIR": str(state.root), "SESSIONDECK_TMUX_SOCKET": socket}
    runtime = TmuxRuntime(state, socket)
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "sessiondeck", "start",
                                                       "--workspace", "personal", "--session", session.id,
                                                       env=env, cwd=directory if newer else elsewhere, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
        assert process.returncode == 0, "Automatic-directory CLI resume failed"
        result = json.loads(stdout)
        assert result["state"] == "ready"
        resumed = Registry(state).get(result["deck_id"])
        assert resumed.registration.directory == directory
        assert resumed.registration.opencode_session_id == session.id
        assert resumed.registration.workspace == "personal"
        assert original_server.returncode is None
    finally:
        runtime.command("kill-server", check=False)
        for conversation in [session, *newer]:
            await client.request("DELETE", f"/session/{conversation.id}", directory=directory)


async def test_live_crud_auth_and_event(live_server):
    client, _, _, directory = live_server
    assert (await client.health())["version"] == "1.19.2"
    async with OpenCodeClient(Endpoint(client.endpoint)) as unauthenticated:
        with pytest.raises(DeckError, match="authentication"):
            await unauthenticated.get_session(directory, "ses_dummy")
    stream = client.events(directory)
    async with asyncio.timeout(20):
        await anext(stream)
        created = await client.create_session(directory, "DECK_PROBE_EVENT")
        try:
            while True:
                event = await anext(stream)
                if created.id in str(event):
                    break
            snapshot = await client.snapshot(directory, created.id)
            assert snapshot.conversation.directory == directory
            assert snapshot.execution == "idle"
            assert not snapshot.permissions and not snapshot.questions
        finally:
            await stream.aclose()
            await client.request("DELETE", f"/session/{created.id}", directory=directory)


async def test_actual_tui_selection(live_server):
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    client, _, env, directory = live_server
    first = await client.create_session(directory, "DECK_PROBE_ALPHA")
    second = await client.create_session(directory, "DECK_PROBE_BETA")
    for session in (first, second):
        await client.request("POST", f"/session/{session.id}/message", directory=directory,
                             body={"noReply": True, "parts": [{"type": "text", "text": session.title}]})
    socket = "deck-live-" + uuid.uuid4().hex

    async def tmux(*args):
        proc = await asyncio.create_subprocess_exec("tmux", "-L", socket, "-f", "/dev/null", *args,
                                                     env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        return out.decode()

    async def wait_title(title):
        screen = ""
        try:
            async with asyncio.timeout(15):
                while True:
                    screen = await tmux("capture-pane", "-p", "-t", "probe:0")
                    if title in screen:
                        return
                    await asyncio.sleep(.25)
        except TimeoutError:
            raise AssertionError(f"Missing {title}; screen:\n{screen}") from None

    try:
        await tmux("new-session", "-d", "-x", "120", "-y", "40", "-s", "probe",
                   "opencode", "attach", client.endpoint, "--pure", "--dir", directory, "--session", first.id)
        await wait_title(first.title)
        assert await client.select_session(directory, second.id)
        await wait_title(second.title)
        assert await client.select_session(directory, first.id)
        await wait_title(first.title)
    finally:
        await tmux("kill-server")
        for session in (first, second):
            await client.request("DELETE", f"/session/{session.id}", directory=directory)
