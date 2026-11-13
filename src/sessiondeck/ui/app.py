import asyncio
import re
import json
import time
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from ..models import DashboardSnapshot, DeckError
from ..monitor import Monitor
from ..runner import start_session
from ..history import list_sessions
from .dialogs import ContinueSessionScreen, CreateSessionScreen, DirectoryScreen, UnloadScreen


def plain(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


def short_path(value) -> str:
    path, home = Path(value), Path.home()
    try:
        return "~" if path == home else "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def age(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def needs_attention(view) -> bool:
    return view.status in {"needs approval", "waiting", "unreachable"}


class SessionDeckApp(App):
    TITLE = "OpenCode SessionDeck"
    CSS = """
    #workspaces { height: 3; }
    #breadcrumb, #hint { height: auto; padding: 0 1; }
    #body { height: 1fr; }
    #sessions { width: 3fr; height: 1fr; }
    #detail-pane { width: 2fr; padding: 1; overflow-y: auto; }
    #details { height: auto; }
    #decisions { height: auto; }
    #decisions Button { min-width: 8; width: 1fr; }
    .narrow #body { layout: vertical; }
    .narrow #sessions { width: 1fr; height: 2fr; }
    .narrow #detail-pane { width: 1fr; height: 1fr; padding: 0 1; }
    .narrow #workspaces Button { min-width: 8; width: 1fr; }
    """
    BINDINGS = [("1", "workspace('work')", "Work"), ("2", "workspace('personal')", "Personal"),
                ("3", "workspace('all')", "All"), ("escape", "back", "Back"),
                ("backspace", "back", "Back"), ("a", "approve", "Allow once"),
                ("d", "deny", "Deny"), ("n", "next_request", "Next request"), ("r", "refresh_data", "Refresh"),
                 ("h", "toggle_completed", "Completed"), ("c", "clear_completed", "Clear ended"),
                ("N", "new_session", "New"), ("C", "continue_session", "Continue"), ("X", "unload_session", "Unload"), ("R", "retry_recovery", "Retry recovery"), ("g", "toggle_grouping", "Grouped")]

    def __init__(self, registry, monitor, runtime):
        super().__init__()
        self.registry, self.monitor, self.runtime = registry, monitor, runtime
        self.workspace = "work"
        self.directory_key = None
        self.snapshot = DashboardSnapshot()
        self.row_sessions = {}
        self.row_projects = {}
        self.selected_ref = None
        self.request_selection = {}
        self.busy_deciding = False
        self.busy_attaching = False
        self.busy_cleaning = False
        self.continue_directory = None
        self.show_completed = False
        self.group_by_directory = True
        self.columns_mode = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspaces"):
            for workspace in ("work", "personal", "all"):
                yield Button(workspace.title(), id=f"workspace-{workspace}")
        yield Label("work / projects", id="breadcrumb")
        with Horizontal(id="body"):
            yield DataTable(id="sessions", cursor_type="row")
            with Vertical(id="detail-pane"):
                yield Static("Waiting for registered sessions…", id="details", markup=False)
                with Horizontal(id="decisions"):
                    yield Button("Allow once", id="allow", disabled=True)
                    yield Button("Deny", id="deny", disabled=True)
        yield Label("Ctrl-Space: agent → list; list → detach. Ctrl-q: detach. Agents keep running.", id="hint")
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).focus()
        self.follow_sessions()
        self.recover_sessions()
        self.set_interval(1, self.recover_sessions)

    @work(group="recovery", exit_on_error=False)
    async def recover_sessions(self):
        from ..recovery import RecoveryCoordinator
        restored = await asyncio.to_thread(RecoveryCoordinator(self.registry, self.runtime).recover_pending)
        if restored:
            self.notify(f"Restored {len(restored)} session(s)")
            self.monitor.request_registry_refresh()

    def on_resize(self):
        self.set_class(self.size.width < 80, "narrow")
        if self.is_mounted:
            self.render_rows()

    @work(group="monitor", exclusive=True, exit_on_error=False)
    async def follow_sessions(self):
        try:
            async for snapshot in self.monitor.watch():
                self.snapshot = snapshot
                self.render_rows()
        except Exception:
            self.query_one("#details", Static).update("Monitoring failed. Restart the dashboard; agents keep running.")

    def current_key(self):
        table = self.query_one(DataTable)
        if table.row_count and table.cursor_row < table.row_count:
            return table.ordered_rows[table.cursor_row].key.value
        return None

    def render_rows(self):
        table = self.query_one(DataTable)
        previous = self.current_key()
        narrow = self.size.width < 80
        views = [v for v in self.snapshot.sessions.values()
                 if (self.workspace == "all" or v.session.registration.workspace == self.workspace)
                 and (self.show_completed or v.status != "stopped")]
        if self.directory_key is not None and not any(
                (view.session.registration.workspace, view.session.registration.directory) == self.directory_key for view in views):
            self.directory_key = None
        mode = (self.directory_key is None and self.group_by_directory, narrow)
        table.clear(columns=mode != self.columns_mode)
        if mode != self.columns_mode:
            self.columns_mode = mode
            if self.directory_key is None and self.group_by_directory:
                table.add_columns(*(["Project", "Attention"] if narrow else ["Workspace", "Project directory", "Sessions", "Attention"]))
            else:
                table.add_columns(*(["State · Session"] if narrow else ["Status", "Session", "Directory", "Activity"]))
        self.row_projects, self.row_sessions = {}, {}
        if self.directory_key is None and self.group_by_directory:
            groups = {}
            for view in views:
                key = (view.session.registration.workspace, view.session.registration.directory)
                groups.setdefault(key, []).append(view)
            for group, sessions in sorted(groups.items()):
                key = json.dumps(group)
                self.row_projects[key] = group
                attention = sum(needs_attention(v) for v in sessions)
                label = f"{group[0]} · {short_path(group[1])}" if self.workspace == "all" else short_path(group[1])
                values = [plain(label), str(attention)] if narrow else [group[0], plain(group[1]), str(len(sessions)), str(attention)]
                table.add_row(*(Text(v) for v in values), key=key)
            breadcrumb = f"{self.workspace} / projects"
        else:
            for view in sorted(views, key=lambda v: (not needs_attention(v), v.title, v.session.deck_id)):
                if self.directory_key is not None and (view.session.registration.workspace, view.session.registration.directory) != self.directory_key:
                    continue
                key = view.session.deck_id
                self.row_sessions[key] = view
                values = [plain(f"{view.status} · {view.title}")] if narrow else [view.status, plain(view.title), short_path(view.session.registration.directory), age(view.last_activity)]
                table.add_row(*(Text(v) for v in values), key=key)
            breadcrumb = (f"{self.directory_key[0]} / {short_path(self.directory_key[1])}" if self.directory_key is not None
                          else f"{self.workspace} / flat")
        if self.show_completed:
            breadcrumb += " · completed shown"
        self.query_one("#breadcrumb", Label).update(Text(breadcrumb))
        keys = [row.key.value for row in table.ordered_rows]
        if previous in keys:
            table.move_cursor(row=keys.index(previous))
        self.update_detail()

    def update_detail(self):
        view = self.row_sessions.get(self.current_key())
        self.selected_ref = None
        if view:
            text = f"{plain(view.title)}\n{view.status}{' (cached)' if view.stale else ''}\n{plain(view.detail)}"
            if view.status == "stopped":
                text += "\n\nCompleted session. Press c to archive completed entries in this view."
            elif view.permissions:
                chosen = self.request_selection.get(view.session.deck_id)
                request = next((request for request in view.permissions if request.ref == chosen), view.permissions[0])
                self.request_selection[view.session.deck_id] = request.ref
                self.selected_ref = request.ref
                index = view.permissions.index(request) + 1
                text += f"\n\nPermission {index}/{len(view.permissions)}: {plain(request.action)}\n"
                text += "\n".join(plain(r) for r in request.resources)
                text += "\n[a] allow once  [d] deny  [n] next"
            elif view.questions:
                text += "\n\n" + "\n".join(plain(q) for request in view.questions for q in request.questions)
                text += "\nEnter to answer in OpenCode"
            text += f"\n\n{plain(view.session.registration.reference_url)}"
        else:
            text = "Enter: open project\nCtrl-b d: detach (agents keep running)\nh: show completed · c: clear completed\n\nLaunch with sessiondeck start, or register an existing tmux session via API v1."
        self.query_one("#details", Static).update(text)
        for name in ("allow", "deny"):
            self.query_one(f"#{name}", Button).disabled = self.selected_ref is None or self.busy_deciding

    def on_data_table_row_highlighted(self, event):
        if self.query(DataTable):
            self.update_detail()

    def on_data_table_row_selected(self, event):
        key = event.row_key.value
        if key in self.row_projects:
            self.directory_key = self.row_projects[key]
            self.render_rows()
        elif key in self.row_sessions:
            if self.row_sessions[key].status == "stopped":
                self.notify("This session has finished. Press c to archive completed entries.")
            elif self.busy_attaching:
                self.notify("An attachment is already pending; you can keep browsing")
            else:
                self.busy_attaching = True
                self.attach_session(key)

    @work(group="attach", exit_on_error=False)
    async def attach_session(self, deck_id):
        try:
            await self.monitor.attach(deck_id)
        except DeckError as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)
        finally:
            self.busy_attaching = False

    def action_workspace(self, workspace):
        self.workspace, self.directory_key = workspace, None
        self.render_rows()

    def action_back(self):
        self.directory_key = None
        self.render_rows()

    def action_refresh_data(self):
        self.monitor.request_registry_refresh()

    def action_new_session(self):
        directory = self.directory_key[1] if self.directory_key else None
        workspace = self.directory_key[0] if self.directory_key else (self.workspace if self.workspace != "all" else "work")
        if directory:
            self.push_screen(CreateSessionScreen(directory, workspace), self._create_selected)
        else:
            self.push_screen(DirectoryScreen(), lambda selected: self._choose_new_directory(selected, workspace))

    def action_continue_session(self):
        if self.directory_key is None:
            self.push_screen(DirectoryScreen(), self._choose_continue_directory)
            return
        self.choose_session(self.directory_key[1])

    def _choose_continue_directory(self, directory):
        if directory:
            self.continue_directory = directory
            self.choose_session(directory)

    @work(group="continue", exit_on_error=False)
    async def choose_session(self, directory):
        try:
            sessions = await asyncio.to_thread(list_sessions, directory)
            if not sessions:
                self.notify("No saved OpenCode sessions in this directory")
                return
            self.push_screen(ContinueSessionScreen(sessions), self._continue_selected)
        except DeckError as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)

    def _continue_selected(self, session_id):
        if not session_id:
            return
        for view in self.snapshot.sessions.values():
            if view.session.registration.opencode_session_id == session_id:
                try:
                    self.runtime.inspect(view.session.binding.pane_id, view.session.binding.generation)
                except DeckError as exc:
                    if exc.code not in {"pane_missing", "stale_runtime", "runtime_missing"}:
                        self.notify(plain(str(exc)), severity="error", timeout=10)
                        return
                    break
                self.busy_attaching = True
                self.attach_session(view.session.deck_id)
                return
        self.busy_attaching = True
        directory = self.directory_key[1] if self.directory_key else self.continue_directory
        workspace = self.directory_key[0] if self.directory_key else (self.workspace if self.workspace != "all" else "work")
        self.continue_session(session_id, directory, workspace)

    @work(group="continue-start", exit_on_error=False)
    async def continue_session(self, session_id, directory, workspace):
        try:
            result = await asyncio.to_thread(start_session, self.registry, self.runtime,
                                             workspace, directory, session_id)
            await self.monitor.attach(result["deck_id"])
        except (DeckError, KeyError) as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)
        finally:
            self.busy_attaching = False
            self.monitor.request_registry_refresh()

    def _choose_new_directory(self, directory, workspace):
        if directory:
            self.push_screen(CreateSessionScreen(directory, workspace), self._create_selected)

    def _create_selected(self, value):
        if value and not self.busy_attaching:
            self.busy_attaching = True
            directory, title, workspace = value
            self.create_session(directory, title, workspace)

    @work(group="create", exit_on_error=False)
    async def create_session(self, directory, title, workspace):
        try:
            result = await asyncio.to_thread(start_session, self.registry, self.runtime, workspace, directory, None, title or None)
            await self.monitor.attach(result["deck_id"])
        except (DeckError, KeyError) as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)
        finally:
            self.busy_attaching = False
            self.monitor.request_registry_refresh()

    def action_retry_recovery(self):
        self.registry.request_recovery()
        self.recover_sessions()

    def action_unload_session(self):
        view = self.row_sessions.get(self.current_key())
        if not view or not view.session.native_runner:
            self.notify("Select a native session to unload", severity="error", timeout=10)
            return
        members = self.registry.binding_members(view.session.deck_id)
        self.push_screen(UnloadScreen(len(members)), lambda confirmed: self._unload_confirmed(view.session.deck_id, members) if confirmed else None)

    def _unload_confirmed(self, deck_id, members):
        self.unload_session(deck_id, members)

    @work(group="unload", exit_on_error=False)
    async def unload_session(self, deck_id, members):
        try:
            group = await asyncio.to_thread(self.registry.begin_unload, deck_id, members)
            await asyncio.to_thread(self.runtime.close_agent_binding, group.binding)
            self.notify(f"Unloaded {len(group.deck_ids)} session(s)")
        except DeckError as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)
        finally:
            self.monitor.request_registry_refresh()

    def action_toggle_completed(self):
        self.show_completed = not self.show_completed
        self.render_rows()

    def action_toggle_grouping(self):
        self.group_by_directory = not self.group_by_directory
        self.directory_key = None
        self.render_rows()

    def action_clear_completed(self):
        if self.busy_cleaning:
            return
        deck_ids = tuple(key for key, view in self.snapshot.sessions.items()
                         if view.status == "stopped"
                         and (self.workspace == "all" or view.session.registration.workspace == self.workspace)
                         and (self.directory_key is None or self.directory_key == (
                             view.session.registration.workspace, view.session.registration.directory)))
        if not deck_ids:
            self.notify("No completed sessions in this view")
            return
        self.busy_cleaning = True
        self.clear_completed(deck_ids)

    @work(group="cleanup", exit_on_error=False)
    async def clear_completed(self, deck_ids):
        try:
            count = await self.monitor.archive_completed(deck_ids)
            self.notify(f"Archived {count} completed session(s)")
        except DeckError as exc:
            self.notify(plain(str(exc)), severity="error", timeout=10)
        finally:
            self.busy_cleaning = False

    def action_next_request(self):
        view = self.row_sessions.get(self.current_key())
        if view and len(view.permissions) > 1:
            index = next((index for index, request in enumerate(view.permissions) if request.ref == self.selected_ref), 0)
            self.request_selection[view.session.deck_id] = view.permissions[(index + 1) % len(view.permissions)].ref
            self.update_detail()

    def decide(self, decision):
        ref = self.selected_ref
        if ref is None or self.busy_deciding:
            return
        self.busy_deciding = True
        self.update_detail()
        self.submit_decision(ref, decision)

    @work(group="decision", exit_on_error=False)
    async def submit_decision(self, ref, decision):
        try:
            result = await self.monitor.decide(ref, decision)
            self.notify(plain(f"{result.outcome}: {result.message}"))
        finally:
            self.busy_deciding = False
            if self.query(DataTable):
                self.update_detail()

    def action_approve(self):
        self.decide("once")

    def action_deny(self):
        self.decide("reject")

    def on_button_pressed(self, event):
        name = event.button.id
        if name.startswith("workspace-"):
            self.action_workspace(name.removeprefix("workspace-"))
        elif name in {"allow", "deny"}:
            self.decide("once" if name == "allow" else "reject")


def run_dashboard(registry, runtime):
    SessionDeckApp(registry, Monitor(registry, runtime), runtime).run()
