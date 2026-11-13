import re
import json
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from ..models import DashboardSnapshot, DeckError
from ..monitor import Monitor


def plain(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


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
                ("h", "toggle_completed", "Completed"), ("c", "clear_completed", "Clear ended")]

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
        self.show_completed = False
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
        yield Label("Ctrl-b d: agent → list; list → detach. Agents keep running.", id="hint")
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).focus()
        self.follow_sessions()

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
        mode = (self.directory_key is None, narrow)
        table.clear(columns=mode != self.columns_mode)
        if mode != self.columns_mode:
            self.columns_mode = mode
            if self.directory_key is None:
                table.add_columns(*(["Project", "Attention"] if narrow else ["Workspace", "Project directory", "Sessions", "Attention"]))
            else:
                table.add_columns(*(["State · Session"] if narrow else ["Status", "Session", "Activity"]))
        self.row_projects, self.row_sessions = {}, {}
        if self.directory_key is None:
            groups = {}
            for view in views:
                key = (view.session.registration.workspace, view.session.registration.directory)
                groups.setdefault(key, []).append(view)
            for group, sessions in sorted(groups.items()):
                key = json.dumps(group)
                self.row_projects[key] = group
                attention = sum(needs_attention(v) for v in sessions)
                label = f"{group[0]} · {group[1]}" if self.workspace == "all" else group[1]
                values = [plain(label), str(attention)] if narrow else [group[0], plain(group[1]), str(len(sessions)), str(attention)]
                table.add_row(*(Text(v) for v in values), key=key)
            breadcrumb = f"{self.workspace} / projects"
        else:
            for view in sorted(views, key=lambda v: (not needs_attention(v), v.title, v.session.deck_id)):
                if (view.session.registration.workspace, view.session.registration.directory) != self.directory_key:
                    continue
                key = view.session.deck_id
                self.row_sessions[key] = view
                values = [plain(f"{view.status} · {view.title}")] if narrow else [view.status, plain(view.title), age(view.last_activity)]
                table.add_row(*(Text(v) for v in values), key=key)
            breadcrumb = f"{self.directory_key[0]} / {plain(self.directory_key[1])}"
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
            self.notify(plain(str(exc)), severity="error")
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

    def action_toggle_completed(self):
        self.show_completed = not self.show_completed
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
            self.notify(plain(str(exc)), severity="error")
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
