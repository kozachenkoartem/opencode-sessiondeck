from dataclasses import replace
import asyncio
import threading
import time

import pytest

from sessiondeck.models import DeckError, PendingPermission, PendingQuestion, PermissionRef, SessionSnapshot
from sessiondeck.monitor import Monitor
from sessiondeck.registry import Registry
from sessiondeck.state import State
from tests.test_registry import binding


class FakeClient:
    def __init__(self, snapshot):
        self.current = snapshot
        self.error = None
        self.timeout_reply = False
        self.replies = []
        self.selected = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def snapshot(self, *_):
        if self.error:
            raise self.error
        return self.current

    async def reply(self, ref, decision):
        self.replies.append((ref, decision))
        self.current = replace(self.current, permissions=())
        if self.timeout_reply:
            raise DeckError("unreachable", "timed out")
        return True

    async def health(self):
        return {"healthy": True, "version": "1.19.2"}

    async def get_session(self, *_):
        return self.current.conversation

    async def select_session(self, directory, session_id):
        self.selected.append((directory, session_id))
        return True

    async def events(self, *_):
        await asyncio.Event().wait()
        yield {}


class FakeRuntime:
    def __init__(self, pane):
        self.pane = pane
        self.focused = []

    def inspect(self, *_):
        return self.pane

    def focus(self, pane):
        self.focused.append(pane)


@pytest.fixture
def monitor_setup(tmp_path):
    registry = Registry(State(tmp_path / "state"))
    reg, verified = binding(tmp_path)
    session = registry.register(reg, verified)
    client = FakeClient(SessionSnapshot(verified.conversation, "idle", (), (), 2000))
    runtime = FakeRuntime(verified.pane)
    monitor = Monitor(registry, runtime, lambda _: client)
    return monitor, client, session


async def test_question_retry_activity_and_outage(monitor_setup):
    monitor, client, session = monitor_setup
    client.current = replace(client.current, execution="busy", questions=(PendingQuestion("que_test", ("Which?",)),))
    view = (await monitor.refresh()).sessions[session.deck_id]
    assert view.status == "waiting" and view.questions
    client.current = replace(client.current, questions=(), execution="retry", retry_message="rate limit")
    assert (await monitor.refresh()).sessions[session.deck_id].detail == "rate limit"
    client.current = replace(client.current, execution="busy")
    before = (await monitor.refresh()).sessions[session.deck_id].last_activity
    assert (await monitor.refresh()).sessions[session.deck_id].last_activity == before
    client.error = DeckError("unreachable", "offline")
    view = (await monitor.refresh()).sessions[session.deck_id]
    assert view.status == "unreachable" and view.stale


def pending(client, session):
    ref = PermissionRef(session.registration.endpoint, session.api_directory, session.registration.opencode_session_id, "per_test")
    client.current = replace(client.current, permissions=(PendingPermission(ref, "bash", ("git status",)),))
    return ref


async def test_permission_identity_and_no_automatic_retry(monitor_setup):
    monitor, client, session = monitor_setup
    ref = pending(client, session)
    assert (await monitor.refresh()).sessions[session.deck_id].status == "needs approval"
    assert (await monitor.decide(replace(ref, request_id="per_stale"), "once")).outcome == "already_handled"
    assert not client.replies
    assert (await monitor.decide(ref, "once")).outcome == "confirmed"
    assert (await monitor.decide(ref, "once")).outcome == "already_handled"
    assert client.replies == [(ref, "once")]


async def test_timeout_remains_unknown_after_request_disappears(monitor_setup):
    monitor, client, session = monitor_setup
    ref = pending(client, session)
    client.timeout_reply = True
    assert (await monitor.decide(ref, "once")).outcome == "unknown"
    await monitor.refresh()
    assert (await monitor.decide(ref, "once")).outcome == "already_handled"
    assert len(client.replies) == 1
    with monitor.registry.connect() as db:
        assert db.execute("SELECT outcome FROM audit").fetchone()[0] == "unknown"


async def test_attach_selects_before_focus(monitor_setup):
    monitor, client, session = monitor_setup
    await monitor.attach(session.deck_id)
    assert client.selected == [(session.api_directory, session.registration.opencode_session_id)]
    assert monitor.runtime.focused == [session.binding]


async def test_empty_question_does_not_break_monitor(monitor_setup):
    monitor, client, session = monitor_setup
    client.current = replace(client.current, questions=(PendingQuestion("que_empty", ()),))
    assert (await monitor.refresh()).sessions[session.deck_id].status == "waiting"


async def test_watch_publishes_healthy_changes_while_another_endpoint_blocks(monitor_setup, tmp_path):
    monitor, fast, first = monitor_setup
    reg, verified = binding(tmp_path, 2)
    slow_session = monitor.registry.register(reg, verified)
    release = asyncio.Event()
    slow_started = asyncio.Event()

    class SlowClient(FakeClient):
        async def snapshot(self, *_):
            slow_started.set()
            await release.wait()
            return self.current

    slow = SlowClient(SessionSnapshot(verified.conversation, "idle", (), (), 2000))
    fast.current = replace(fast.current, execution="busy")
    monitor.client_factory = lambda registration: fast if registration.endpoint == first.registration.endpoint else slow
    stream = monitor.watch()
    try:
        async with asyncio.timeout(1):
            while (snapshot := await anext(stream)).sessions.get(first.deck_id) is None or snapshot.sessions[first.deck_id].status != "working":
                pass
        assert slow_started.is_set() and not release.is_set()
        fast.current = replace(fast.current, execution="idle")
        monitor.dirty.set()
        async with asyncio.timeout(2):
            while (snapshot := await anext(stream)).sessions[first.deck_id].status != "idle":
                pass
        assert not release.is_set()
    finally:
        release.set()
        await stream.aclose()


async def test_permission_uses_live_binding_after_endpoint_reuse(monitor_setup):
    from sessiondeck.models import VerifiedBinding
    monitor, client, old = monitor_setup
    ref = pending(client, old)
    pane = replace(old.binding, pane_id="%20", window_id="@20", generation="replacement-runtime")
    registration = replace(old.registration, tmux_pane_id=pane.pane_id, tmux_generation=pane.generation)
    current = monitor.registry.register(registration, VerifiedBinding(pane, client.current.conversation),
                                         obsolete_bindings={old.deck_id: old.binding})

    def inspect(pane_id, generation):
        if pane_id == pane.pane_id and generation == pane.generation:
            return pane
        raise DeckError("stale_runtime", "Previous runtime")

    monitor.runtime.inspect = inspect
    assert (await monitor.decide(ref, "once")).outcome == "confirmed"
    assert client.replies == [(ref, "once")]
    assert not monitor.registry.get(old.deck_id).retired
    assert current.deck_id != old.deck_id


async def test_native_exit_is_stopped_while_pane_is_still_cleaning_up(monitor_setup):
    monitor, client, session = monitor_setup
    launch = monitor.registry.create_launch({})
    monitor.registry.update_launch(launch["id"], state="stopped", deck_id=session.deck_id)
    client.error = DeckError("unreachable", "Backend shutting down")
    assert (await monitor.refresh()).sessions[session.deck_id].status == "stopped"


async def test_archive_requests_immediate_registry_rescan(monitor_setup):
    monitor, _, session = monitor_setup
    stream = monitor.watch()
    try:
        assert session.deck_id in (await anext(stream)).sessions
        monitor.registry.set_archived(session.deck_id, True)
        monitor.request_registry_refresh()
        async with asyncio.timeout(1):
            while session.deck_id in (await anext(stream)).sessions:
                pass
    finally:
        await stream.aclose()


async def test_cleanup_rechecks_status_and_preserves_live_or_unreachable(monitor_setup):
    monitor, client, session = monitor_setup
    client.current = replace(client.current, execution="busy")
    assert await monitor.archive_completed((session.deck_id,)) == 0
    assert not monitor.registry.get(session.deck_id).archived
    client.error = DeckError("unreachable", "Temporary network failure")
    assert await monitor.archive_completed((session.deck_id,)) == 0
    assert not monitor.registry.get(session.deck_id).archived
    monitor.runtime.pane = replace(monitor.runtime.pane, dead=True)
    assert await monitor.archive_completed((session.deck_id,)) == 1
    assert monitor.registry.get(session.deck_id).archived
    assert not monitor.runtime.focused


async def test_unverifiable_runtime_is_not_archived_as_completed(monitor_setup):
    monitor, _, session = monitor_setup

    def unavailable(*_):
        raise DeckError("runtime_missing", "Runtime marker is unavailable")

    monitor.runtime.inspect = unavailable
    assert (await monitor.refresh()).sessions[session.deck_id].status == "unreachable"
    assert await monitor.archive_completed((session.deck_id,)) == 0
    assert not monitor.registry.get(session.deck_id).archived


async def test_completed_native_job_can_be_cleared_without_runtime_marker(monitor_setup):
    monitor, _, session = monitor_setup
    launch = monitor.registry.create_launch({})
    monitor.registry.update_launch(launch["id"], state="stopped", deck_id=session.deck_id)

    def unavailable(*_):
        raise DeckError("runtime_missing", "Runtime marker is unavailable")

    monitor.runtime.inspect = unavailable
    assert (await monitor.refresh()).sessions[session.deck_id].status == "stopped"
    assert await monitor.archive_completed((session.deck_id,)) == 1


@pytest.mark.parametrize("stopped", [True, False])
async def test_cleanup_database_contention_does_not_block_ui_loop(monitor_setup, stopped):
    monitor, _, session = monitor_setup
    monitor.runtime.pane = replace(monitor.runtime.pane, dead=stopped)
    locked, release = threading.Event(), threading.Event()

    def writer():
        with monitor.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(2)

    thread = threading.Thread(target=writer)
    thread.start()
    await asyncio.to_thread(locked.wait)
    timer = threading.Timer(.6, release.set)
    timer.start()
    cleanup = asyncio.create_task(monitor.archive_completed((session.deck_id,)))
    try:
        started = time.monotonic()
        await asyncio.sleep(.03)
        assert time.monotonic() - started < .3
    finally:
        release.set()
        timer.cancel()
        thread.join()
        result = await cleanup
    assert result == int(stopped)


async def test_recycled_endpoint_reopens_events_with_new_credentials(monitor_setup):
    from sessiondeck.models import VerifiedBinding
    monitor, client, old = monitor_setup
    subscriptions = []

    class EventClient(FakeClient):
        def __init__(self, registration):
            super().__init__(client.current)
            self.registration = registration

        async def events(self, *_):
            subscriptions.append(self.registration.credential_ref)
            await asyncio.Event().wait()
            yield {}

    monitor.client_factory = EventClient
    stream = monitor.watch()
    try:
        async with asyncio.timeout(2):
            while not subscriptions:
                await anext(stream)
        pane = replace(old.binding, pane_id="%30", window_id="@30", generation="new-runtime")
        credential = monitor.registry.state.save_credential("opencode", "fixture-new-password")
        registration = replace(old.registration, tmux_pane_id=pane.pane_id,
                               tmux_generation=pane.generation, credential_ref=credential)
        monitor.registry.register(registration, VerifiedBinding(pane, client.current.conversation),
                                   obsolete_bindings={old.deck_id: old.binding})

        def inspect(pane_id, generation):
            if pane_id == pane.pane_id and generation == pane.generation:
                return pane
            raise DeckError("stale_runtime", "Previous runtime")

        monitor.runtime.inspect = inspect
        monitor.request_registry_refresh()
        async with asyncio.timeout(2):
            while credential not in subscriptions:
                await anext(stream)
        assert subscriptions == [None, credential]
    finally:
        await stream.aclose()
