from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Input, Label


class CreateSessionScreen(ModalScreen[tuple[str, str, str] | None]):
    def __init__(self, directory: str, workspace: str):
        super().__init__()
        self.directory = directory
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label(f"New session in {self.directory}")
            yield Input(value=self.workspace, placeholder="work or personal", id="workspace")
            yield Input(placeholder="Session name (optional)", id="title")
            yield Button("Create", id="create", variant="success")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "title":
            self._create()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "create":
            self._create()
        else:
            self.dismiss(None)

    def _create(self):
        workspace = self.query_one("#workspace", Input).value.strip()
        if workspace not in {"work", "personal"}:
            self.notify("Workspace must be work or personal", severity="error", timeout=10)
            return
        self.dismiss((self.directory, self.query_one("#title", Input).value.strip(), workspace))


class DirectoryScreen(ModalScreen[str | None]):
    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label("Project directory")
            yield DirectoryTree(str(Path.home()), id="directories")
            yield Input(value=str(Path.home()), placeholder="Existing directory", id="directory")
            yield Button("Select", id="select", variant="success")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#directory", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        self._select(event.value)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "select":
            self._select(self.query_one("#directory", Input).value)
        else:
            self.dismiss(None)

    def _select(self, value: str):
        try:
            directory = Path(value).expanduser().resolve(strict=True)
        except OSError:
            self.notify("Directory does not exist", severity="error", timeout=10)
            return
        if not directory.is_dir():
            self.notify("Select a directory", severity="error", timeout=10)
            return
        self.dismiss(str(directory))

    def on_tree_node_selected(self, event):
        path = getattr(event.node.data, "path", None)
        if path is not None and Path(path).is_dir():
            self.query_one("#directory", Input).value = str(path)


class UnloadScreen(ModalScreen[bool]):
    def __init__(self, count: int):
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label(f"Unload {self.count} session(s)? Their OpenCode history is kept.")
            yield Button("Unload", id="unload", variant="error")
            yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "unload")


class ContinueSessionScreen(ModalScreen[str | None]):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label("Continue OpenCode session")
            yield DataTable(id="sessions", cursor_type="row")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        table = self.query_one("#sessions", DataTable)
        table.add_columns("Session", "Updated")
        for session in self.sessions:
            table.add_row(session["title"], str(session["updated"]), key=session["id"])
        table.focus()

    def on_data_table_row_selected(self, event):
        self.dismiss(event.row_key.value)

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(None)
