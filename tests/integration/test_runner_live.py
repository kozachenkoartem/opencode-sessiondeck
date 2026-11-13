import asyncio
import json
import os
import signal
import sys
import shutil
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from sessiondeck.models import Endpoint
from sessiondeck.registration import register_session
from sessiondeck.monitor import Monitor
from sessiondeck.opencode import OpenCodeClient
from sessiondeck.registry import Registry
from sessiondeck.runner import start_session
from sessiondeck.state import State
from sessiondeck.tmux import TmuxRuntime

pytestmark = pytest.mark.live


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except FileNotFoundError:
        return False


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.1)


@pytest.fixture
def native_runtime(tmp_path, monkeypatch, local_provider):
    for key in ("CONFIG", "DATA", "CACHE", "STATE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    provider = {"sessiondeck-fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                "options": {"baseURL": local_provider, "apiKey": "local-fixture"},
                "models": {"fixture": {"name": "Fixture", "limit": {"context": 8192, "output": 512}}}}}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"plugin": [], "mcp": {}, "share": "disabled",
                       "provider": provider, "permission": {"bash": "ask"},
                       "model": "sessiondeck-fixture/fixture", "small_model": "sessiondeck-fixture/fixture"}))
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "1")
    monkeypatch.setenv("OPENCODE_PURE", "1")
    monkeypatch.setenv("OPENCODE_DISABLE_AUTOUPDATE", "1")
    registry = Registry(State(tmp_path / "deck-state"))
    runtime = TmuxRuntime(registry.state, "deck-run-" + uuid.uuid4().hex)
    try:
        yield registry, runtime
    finally:
        runtime.command("kill-server", check=False)


async def test_native_launch_recovery_and_window_loss(native_runtime, tmp_path):
    registry, runtime = native_runtime
    attempt = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path), title="DECK_NATIVE")
    assert attempt["state"] == "ready"
    session = registry.get(attempt["deck_id"])
    backend_pid = attempt["backend_pid"]
    assert running(backend_pid)
    auth = registry.state.auth(session.registration.credential_ref)
    async with OpenCodeClient(Endpoint(session.registration.endpoint), auth=auth) as client:
        await client.request("POST", f"/session/{session.registration.opencode_session_id}/message", directory=session.api_directory,
                             body={"noReply": True, "parts": [{"type": "text", "text": "DECK_NATIVE_VISIBLE"}]})
    await until(lambda: "DECK_NATIVE_VISIBLE" in runtime.command("capture-pane", "-p", "-t", session.binding.pane_id).stdout)
    dashboard = runtime.ensure().dashboard_pane_id
    runtime.command("kill-pane", "-t", dashboard)
    assert runtime.ensure().dashboard_pane_id != dashboard
    assert running(backend_pid)
    os.kill(attempt["tui_pid"], signal.SIGKILL)
    await until(lambda: registry.launch(attempt["id"])["state"] == "tui_lost")
    await Monitor(registry, runtime).attach(session.deck_id)
    recovered = registry.launch(attempt["id"])
    assert recovered["backend_pid"] == backend_pid
    assert recovered["tui_pid"] != attempt["tui_pid"]
    await until(lambda: "DECK_NATIVE_VISIBLE" in runtime.command("capture-pane", "-p", "-t", session.binding.pane_id).stdout)
    runtime.command("send-keys", "-l", "-t", session.binding.pane_id, "RECOVERED_INPUT")
    await until(lambda: "RECOVERED_INPUT" in runtime.command("capture-pane", "-p", "-t", session.binding.pane_id).stdout)
    runtime.command("kill-window", "-t", session.binding.window_id)
    await until(lambda: not running(backend_pid))


async def test_abrupt_runner_loss_kills_backend(native_runtime, tmp_path):
    registry, runtime = native_runtime
    attempt = await asyncio.to_thread(start_session, registry, runtime, "personal", str(tmp_path))
    assert attempt["state"] == "ready"
    os.kill(attempt["runner_pid"], signal.SIGKILL)
    await until(lambda: not running(attempt["backend_pid"]))
    await until(lambda: not running(attempt["tui_pid"]))


async def test_parallel_projects_and_historical_resume(native_runtime, tmp_path):
    from sessiondeck.models import DeckError
    registry, runtime = native_runtime
    attempts = []
    for name, workspace in [("app-a", "work"), ("app-b", "work"), ("home", "personal")]:
        directory = tmp_path / name
        directory.mkdir()
        attempts.append(await asyncio.to_thread(start_session, registry, runtime, workspace, str(directory)))
    assert len({a["endpoint"] for a in attempts}) == 3
    assert all(running(a["backend_pid"]) for a in attempts)
    views = (await Monitor(registry, runtime).refresh()).sessions
    assert len(views) == 3
    assert len(registry.list("work")) == 2 and len(registry.list("personal")) == 1
    first = registry.get(attempts[0]["deck_id"])
    with pytest.raises(DeckError, match="already managed"):
        await asyncio.to_thread(start_session, registry, runtime, "work", first.registration.directory,
                                session_id=first.registration.opencode_session_id)
    runtime.command("kill-window", "-t", first.binding.window_id)
    await until(lambda: not running(attempts[0]["backend_pid"]))
    registry.unregister(first.deck_id)
    resumed = await asyncio.to_thread(start_session, registry, runtime, "work", first.registration.directory,
                                      session_id=first.registration.opencode_session_id)
    assert registry.get(resumed["deck_id"]).registration.opencode_session_id == first.registration.opencode_session_id
    assert all(running(a["backend_pid"]) for a in attempts[1:])


async def test_secondary_conversation_recovers_shared_tui(native_runtime, tmp_path):
    registry, runtime = native_runtime
    attempt = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path))
    first = registry.get(attempt["deck_id"])
    async with OpenCodeClient(Endpoint(first.registration.endpoint), auth=registry.state.auth(first.registration.credential_ref)) as client:
        second = await client.create_session(first.api_directory, "SECONDARY_RECOVERY")
        await client.request("POST", f"/session/{second.id}/message", directory=first.api_directory,
                             body={"noReply": True, "parts": [{"type": "text", "text": "SECONDARY_RECOVERY_VISIBLE"}]})
    managed = await register_session(registry, runtime, replace(first.registration, opencode_session_id=second.id))
    os.kill(attempt["tui_pid"], signal.SIGKILL)
    await until(lambda: registry.launch(attempt["id"])["state"] == "tui_lost")
    await Monitor(registry, runtime).attach(managed.deck_id)
    recovered = registry.launch(attempt["id"])
    assert recovered["state"] == "ready"
    assert recovered["backend_pid"] == attempt["backend_pid"]
    await until(lambda: "SECONDARY_RECOVERY_VISIBLE" in runtime.command("capture-pane", "-p", "-t", first.binding.pane_id).stdout)


async def test_external_registration_json_cli(native_runtime, tmp_path):
    registry, runtime = native_runtime
    attempt = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path))
    session = registry.get(attempt["deck_id"])
    payload = asdict(session.registration)
    payload.pop("credential_ref")
    payload["auth_env"] = "DECK_TEST_PASSWORD"
    password = registry.state.auth(session.registration.credential_ref)[1]
    env = os.environ | {"SESSIONDECK_STATE_DIR": str(registry.state.root),
                        "SESSIONDECK_TMUX_SOCKET": runtime.socket_name, "DECK_TEST_PASSWORD": password}
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "sessiondeck", "api", "v1", "register",
                                                   env=env, stdin=asyncio.subprocess.PIPE,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate(json.dumps(payload).encode())
    assert process.returncode == 0
    assert json.loads(stdout)["data"]["deck_id"] == session.deck_id
    assert password.encode() not in stdout + stderr


@pytest.mark.parametrize("restart_runtime", [False, True])
async def test_resume_after_old_binding_stops_preserves_history(native_runtime, tmp_path, restart_runtime):
    registry, runtime = native_runtime
    first = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path))
    old = registry.get(first["deck_id"])
    if restart_runtime:
        runtime.command("kill-server")
    else:
        runtime.command("kill-pane", "-t", old.binding.pane_id)
    await until(lambda: not running(first["backend_pid"]))
    resumed = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path),
                                      session_id=old.registration.opencode_session_id)
    new = registry.get(resumed["deck_id"])
    assert new.registration.opencode_session_id == old.registration.opencode_session_id
    assert not registry.get(old.deck_id).retired
    assert old.deck_id != new.deck_id
    assert len(registry.list()) == 2


async def test_concurrent_resumes_hold_startup_claim_after_caller_timeout(native_runtime, tmp_path, monkeypatch):
    from sessiondeck import runner

    registry, runtime = native_runtime
    original = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path))
    old = registry.get(original["deck_id"])
    runtime.command("kill-pane", "-t", old.binding.pane_id)
    await until(lambda: not running(original["backend_pid"]))
    gate = tmp_path / "allow-backend-start"
    wrapper = tmp_path / "gated-opencode"
    executable = shutil.which("opencode")
    wrapper.write_text(f'''#!{sys.executable}
import os,sys,time
from pathlib import Path
if len(sys.argv) > 1 and sys.argv[1] == "serve":
    while not Path({str(gate)!r}).exists():
        time.sleep(.02)
os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o755)
    runtime.command("set-environment", "-t", "main", "SESSIONDECK_OPENCODE", str(wrapper))
    monkeypatch.setattr(runner, "START_TIMEOUT", .1)
    first = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path),
                                    session_id=old.registration.opencode_session_id)
    await until(lambda: bool(registry.launch(first["id"]).get("backend_pid")))
    second = await asyncio.to_thread(start_session, registry, runtime, "work", str(tmp_path),
                                     session_id=old.registration.opencode_session_id)
    try:
        assert first["state"] == second["state"] == "starting"
        await until(lambda: bool(registry.launch(second["id"]).get("waiting_for_resume_lock")
                                or registry.launch(second["id"]).get("backend_pid")))
        assert registry.launch(second["id"]).get("backend_pid") is None
    finally:
        gate.touch()
    await until(lambda: registry.launch(first["id"])["state"] == "ready")
    await until(lambda: registry.launch(second["id"])["state"] == "failed")
    assert registry.launch(second["id"]).get("backend_pid") is None
    assert len(registry.list()) == 2
