import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .models import DeckError, ManagedSession, PaneBinding, Registration, VerifiedBinding
from .opencode import normalize_endpoint
from .state import State


class Registry:
    def __init__(self, state: State):
        self.state = state
        self.path = state.root / "registry.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise DeckError("unsupported_schema", "Unsupported SessionDeck registry schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    deck_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                    directory TEXT NOT NULL, endpoint TEXT NOT NULL,
                    session_id TEXT NOT NULL, socket TEXT NOT NULL,
                    generation TEXT NOT NULL, pane_id TEXT NOT NULL,
                    registration TEXT NOT NULL, binding TEXT NOT NULL,
                    api_directory TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    retired INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    last_activity REAL NOT NULL, cached_title TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS launches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, decision TEXT NOT NULL,
                    outcome TEXT NOT NULL, created_at REAL NOT NULL
                );
                PRAGMA user_version=1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row) -> ManagedSession:
        return ManagedSession(row["deck_id"], Registration(**json.loads(row["registration"])),
                              row["api_directory"], PaneBinding(**json.loads(row["binding"])),
                              bool(row["archived"]), bool(row["retired"]), row["created_at"], row["last_activity"])

    def register(self, registration: Registration, verified_binding: VerifiedBinding, *,
                 obsolete_bindings: dict[str, PaneBinding] | None = None) -> ManagedSession:
        if registration.workspace not in {"work", "personal"}:
            raise DeckError("invalid_workspace", "Workspace must be work or personal", 2)
        try:
            directory = str(Path(registration.directory).expanduser().resolve(strict=True))
            if not Path(directory).is_dir():
                raise OSError
        except (OSError, RuntimeError):
            raise DeckError("invalid_directory", "Project directory does not exist", 2) from None
        endpoint = normalize_endpoint(registration.endpoint)
        registration = replace(registration, directory=directory, endpoint=endpoint,
                               tmux_socket=str(Path(registration.tmux_socket).resolve()))
        pane, conversation = verified_binding.pane, verified_binding.conversation
        if (pane.dead or pane.pane_id != registration.tmux_pane_id or
                pane.generation != registration.tmux_generation or
                pane.socket_path != registration.tmux_socket or
                conversation.id != registration.opencode_session_id or
                str(Path(conversation.directory).resolve()) != directory):
            raise DeckError("binding_mismatch", "Registration identity does not match live resources", 4)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM sessions WHERE retired=0").fetchall()
            for row in rows:
                obsolete = obsolete_bindings.get(row["deck_id"]) if obsolete_bindings else None
                if obsolete is not None and obsolete.socket_path == pane.socket_path and self.decode(row).binding == obsolete:
                    continue
                same_pane = (row["socket"], row["generation"], row["pane_id"]) == (
                    pane.socket_path, pane.generation, pane.pane_id)
                same_endpoint = row["endpoint"] == endpoint
                if same_pane != same_endpoint:
                    raise DeckError("binding_conflict", "Pane/endpoint is already bound; unregister the old binding first", 4)
                if same_pane:
                    if row["workspace"] != registration.workspace or row["directory"] != directory:
                        raise DeckError("binding_conflict", "Binding belongs to another workspace or directory", 4)
                    if row["session_id"] == conversation.id:
                        return self.decode(row)
            deck_id = str(uuid.uuid4())
            db.execute("""INSERT INTO sessions
                (deck_id,workspace,directory,endpoint,session_id,socket,generation,pane_id,
                 registration,binding,api_directory,created_at,last_activity,cached_title)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    deck_id, registration.workspace, directory, endpoint, conversation.id,
                    pane.socket_path, pane.generation, pane.pane_id, json.dumps(asdict(registration)),
                    json.dumps(asdict(pane)), conversation.directory, now, conversation.updated_ms / 1000,
                    conversation.title or registration.title))
            return self.decode(db.execute("SELECT * FROM sessions WHERE deck_id=?", (deck_id,)).fetchone())

    def list(self, workspace="all", directory=None, include_archived=False, include_retired=False) -> list[ManagedSession]:
        query, args = "SELECT * FROM sessions WHERE 1=1", []
        if not include_retired:
            query += " AND retired=0"
        if not include_archived:
            query += " AND archived=0"
        if workspace != "all":
            query += " AND workspace=?"
            args.append(workspace)
        if directory is not None:
            query += " AND directory=?"
            args.append(str(Path(directory).expanduser().resolve()))
        with self.connect() as db:
            return [self.decode(row) for row in db.execute(query + " ORDER BY created_at", args)]

    def get(self, deck_id: str) -> ManagedSession:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE deck_id=?", (deck_id,)).fetchone()
            if not row:
                raise DeckError("not_found", "Unknown SessionDeck ID", 2)
            return self.decode(row)

    def set_archived(self, deck_id: str, value: bool):
        self.get(deck_id)
        with self.connect() as db:
            db.execute("UPDATE sessions SET archived=? WHERE deck_id=?", (value, deck_id))

    def unregister(self, deck_id: str):
        self.get(deck_id)
        with self.connect() as db:
            db.execute("UPDATE sessions SET retired=1 WHERE deck_id=?", (deck_id,))

    def cache(self, deck_id: str, title: str, activity: float):
        with self.connect() as db:
            db.execute("UPDATE sessions SET cached_title=?,last_activity=? WHERE deck_id=?", (title, activity, deck_id))

    def cached_title(self, deck_id: str) -> str:
        with self.connect() as db:
            return db.execute("SELECT cached_title FROM sessions WHERE deck_id=?", (deck_id,)).fetchone()[0]

    def create_launch(self, data: dict) -> dict:
        launch_id = str(uuid.uuid4())
        data = dict(data, id=launch_id, state="starting", restart_tui=False)
        with self.connect() as db:
            db.execute("INSERT INTO launches VALUES (?,?)", (launch_id, json.dumps(data)))
        return data

    def launch(self, launch_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM launches WHERE id=?", (launch_id,)).fetchone()
            if not row:
                raise DeckError("not_found", "Unknown launch ID", 2)
            return json.loads(row[0])

    def update_launch(self, launch_id: str, **updates) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM launches WHERE id=?", (launch_id,)).fetchone()
            if not row:
                raise DeckError("not_found", "Unknown launch ID", 2)
            data = json.loads(row[0]) | updates
            db.execute("UPDATE launches SET data=? WHERE id=?", (json.dumps(data), launch_id))
            return data

    def consume_tui_restart(self, launch_id: str) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM launches WHERE id=?", (launch_id,)).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            requested = data.get("restart_tui", False)
            session_id = data.pop("restart_session_id", None)
            data["restart_tui"] = False
            db.execute("UPDATE launches SET data=? WHERE id=?", (json.dumps(data), launch_id))
            return {"session_id": session_id} if requested else None

    def launch_for(self, deck_id: str) -> dict | None:
        target = self.get(deck_id)
        with self.connect() as db:
            for row in db.execute("SELECT data FROM launches"):
                launch = json.loads(row[0])
                if launch.get("deck_id") == deck_id:
                    return launch
                if launch.get("deck_id"):
                    owner = db.execute("SELECT * FROM sessions WHERE deck_id=?", (launch["deck_id"],)).fetchone()
                    if owner and (owner["endpoint"], owner["socket"], owner["generation"], owner["pane_id"]) == (
                            target.registration.endpoint, target.binding.socket_path, target.binding.generation, target.binding.pane_id):
                        return launch
        return None

    def audit_attempt(self, ref, decision: str) -> str:
        audit_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (audit_id, json.dumps(asdict(ref)), decision, "attempted", time.time()))
        return audit_id

    def audit_outcome(self, audit_id: str, outcome: str):
        with self.connect() as db:
            db.execute("UPDATE audit SET outcome=? WHERE id=?", (outcome, audit_id))
