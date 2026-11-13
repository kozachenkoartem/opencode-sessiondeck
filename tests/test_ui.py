import asyncio
from dataclasses import replace

import pytest
from textual.widgets import DataTable, Static
from textual import events

from sessiondeck.models import DashboardSnapshot, DecisionResult, SessionView
from sessiondeck.ui.app import SessionDeckApp, plain, short_path
from tests.test_monitor import monitor_setup, pending
from tests.test_registry import binding


class StaticMonitor:
    def __init__(self, snapshot):
        self.value = snapshot
        self.dirty = asyncio.Event()
        self.attached = []

    async def watch(self):
        yield self.value
        await asyncio.Event().wait()

    async def attach(self, deck_id):
        self.attached.append(deck_id)

    def request_registry_refresh(self):
        self.dirty.set()


def test_short_path_uses_home_marker(monkeypatch):
    monkeypatch.setattr("sessiondeck.ui.app.Path.home", lambda: __import__("pathlib").Path("/home/tester"))
    assert short_path("/home/tester/ptaf") == "~/ptaf"


async def test_directory_navigation_and_small_terminal(monitor_setup):
    monitor, client, session = monitor_setup
    snapshot = await monitor.refresh()
    static = StaticMonitor(snapshot)
    app = SessionDeckApp(monitor.registry, static, monitor.runtime)
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.directory_key == ("work", session.registration.directory)
        assert app.query_one(DataTable).row_count == 1
        await pilot.press("enter")
        assert static.attached == [session.deck_id]
        await pilot.press("escape", "2")
        assert app.directory_key is None and app.workspace == "personal"
        assert app.query_one(DataTable).row_count == 0


async def test_multiple_requests_and_plain_text(monitor_setup):
    monitor, client, session = monitor_setup
    ref = pending(client, session)
    first = client.current.permissions[0]
    second = replace(first, ref=replace(ref, request_id="per_second"), resources=("[bold]not markup\x1b[31m",))
    client.current = replace(client.current, permissions=(first, second))
    snapshot = await monitor.refresh()
    static = StaticMonitor(snapshot)
    app = SessionDeckApp(monitor.registry, static, monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_ref == ref
        await pilot.press("n")
        assert app.selected_ref == second.ref
        app.render_rows()
        assert app.selected_ref == second.ref
        assert "[bold]not markup" in str(app.query_one("#details", Static).content)
        assert "\x1b" not in plain("bad\x1b[31m")


async def test_project_selection_survives_insert(monitor_setup):
    monitor, _, session = monitor_setup
    snapshot = await monitor.refresh()
    app = SessionDeckApp(monitor.registry, StaticMonitor(snapshot), monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        selected_group = app.row_projects[app.current_key()]
        other = replace(session, deck_id="other", registration=replace(session.registration, directory="/aaa-new-project"))
        view = replace(snapshot.sessions[session.deck_id], session=other)
        app.snapshot = DashboardSnapshot(snapshot.sessions | {"other": view})
        app.render_rows()
        assert app.row_projects[app.current_key()] == selected_group


async def test_retry_counts_as_project_attention(monitor_setup):
    monitor, client, session = monitor_setup
    client.current = replace(client.current, execution="retry", retry_message="rate limited")
    app = SessionDeckApp(monitor.registry, StaticMonitor(await monitor.refresh()), monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).get_row_at(0)[-1].plain == "1"


@pytest.mark.parametrize("operation", ["attach", "decision"])
async def test_navigation_is_responsive_during_pending_operation(monitor_setup, operation):
    monitor, client, session = monitor_setup
    ref = pending(client, session)
    started, release, switched, finished = (asyncio.Event() for _ in range(4))

    class BlockingMonitor(StaticMonitor):
        async def attach(self, deck_id):
            started.set()
            await release.wait()
            finished.set()

        async def decide(self, captured_ref, decision):
            assert captured_ref == ref and decision == "once"
            started.set()
            await release.wait()
            finished.set()
            return DecisionResult("confirmed")

    class TrackingApp(SessionDeckApp):
        def action_workspace(self, workspace):
            super().action_workspace(workspace)
            if workspace == "personal":
                switched.set()

    app = TrackingApp(monitor.registry, BlockingMonitor(await monitor.refresh()), monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        press = asyncio.create_task(pilot.press("enter" if operation == "attach" else "a"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            app.post_message(events.Key("2", "2"))
            await asyncio.wait_for(switched.wait(), 1)
            assert app.workspace == "personal" and not finished.is_set()
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await press


async def test_completed_hidden_unreachable_visible_and_no_attach(monitor_setup):
    monitor, _, session = monitor_setup
    original = (await monitor.refresh()).sessions[session.deck_id]
    stopped = replace(original, status="stopped")
    static = StaticMonitor(DashboardSnapshot({session.deck_id: stopped}))
    app = SessionDeckApp(monitor.registry, static, monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        await pilot.press("h")
        assert app.query_one(DataTable).row_count == 1
        await pilot.press("enter", "enter")
        assert not static.attached
        app.snapshot = DashboardSnapshot({session.deck_id: replace(original, status="unreachable", stale=True)})
        app.render_rows()
        await pilot.press("h", "c")
        assert app.query_one(DataTable).row_count == 1
        assert not monitor.registry.get(session.deck_id).archived


async def test_cleanup_only_archives_completed_in_current_workspace(monitor_setup, tmp_path):
    monitor, _, completed = monitor_setup
    snapshot = await monitor.refresh()
    views = {completed.deck_id: replace(snapshot.sessions[completed.deck_id], status="stopped")}
    added = {}
    for number, workspace, status in [(2, "work", "working"), (3, "personal", "stopped"), (4, "work", "unreachable")]:
        session = monitor.registry.register(*binding(tmp_path, number, workspace))
        added[number] = session
        views[session.deck_id] = SessionView(session, status, "Fixture", 1)

    class UpdatingMonitor(StaticMonitor):
        async def archive_completed(self, deck_ids):
            for deck_id in deck_ids:
                monitor.registry.set_archived(deck_id, True)
            self.request_registry_refresh()
            return len(deck_ids)

        async def watch(self):
            yield self.value
            while True:
                await self.dirty.wait()
                self.dirty.clear()
                active = {session.deck_id for session in monitor.registry.list()}
                yield DashboardSnapshot({key: view for key, view in views.items() if key in active})

    app = SessionDeckApp(monitor.registry, UpdatingMonitor(DashboardSnapshot(views)), monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h", "c")
        await pilot.pause()
        assert monitor.registry.get(completed.deck_id).archived
        assert all(not monitor.registry.get(session.deck_id).archived for session in added.values())
        assert app.query_one(DataTable).row_count == 2
        assert not monitor.runtime.focused


async def test_dashboard_can_quit_without_archiving_agents(monitor_setup):
    monitor, _, session = monitor_setup
    app = SessionDeckApp(monitor.registry, StaticMonitor(await monitor.refresh()), monitor.runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
    assert not monitor.registry.get(session.deck_id).archived
    assert not monitor.runtime.focused
