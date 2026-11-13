import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .models import DeckError, LifecycleClaim, ManagedSession, PaneBinding, Registration, UnloadGroup, VerifiedBinding
from .opencode import normalize_endpoint
from .state import State


class Registry:
    def __init__(self, state: State):
        self.state = state
        self.path = state.root / "registry.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3}:
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
                    last_activity REAL NOT NULL, cached_title TEXT NOT NULL,
                    native_runner INTEGER NOT NULL DEFAULT 1,
                    restore_on_start INTEGER NOT NULL DEFAULT 1,
                    lifecycle_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS launches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lifecycle_claims (deck_id TEXT PRIMARY KEY, token TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS closing_bindings (socket TEXT NOT NULL, generation TEXT NOT NULL, pane_id TEXT NOT NULL,
                    PRIMARY KEY (socket, generation, pane_id));
                CREATE TABLE IF NOT EXISTS recovery_requests (id INTEGER PRIMARY KEY CHECK (id=1), requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, decision TEXT NOT NULL,
                    outcome TEXT NOT NULL, created_at REAL NOT NULL
                );
                INSERT OR IGNORE INTO recovery_requests(id, requested) VALUES (1, 0);
                """)
            if version < 2:
                columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
                for name, definition in (("native_runner", "INTEGER NOT NULL DEFAULT 1"),
                                         ("restore_on_start", "INTEGER NOT NULL DEFAULT 1"),
                                         ("lifecycle_revision", "INTEGER NOT NULL DEFAULT 0")):
                    if name not in columns:
                        db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
                db.execute("PRAGMA user_version=2")
            if version < 3:
                db.execute("UPDATE sessions SET native_runner=0,restore_on_start=0")
                for launch in db.execute("SELECT data FROM launches"):
                    deck_id = json.loads(launch[0]).get("deck_id")
                    if deck_id:
                        db.execute("UPDATE sessions SET native_runner=1,restore_on_start=1 WHERE deck_id=?", (deck_id,))
                db.execute("PRAGMA user_version=3")
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
                               bool(row["archived"]), bool(row["retired"]), row["created_at"], row["last_activity"],
                               bool(row["native_runner"]), bool(row["restore_on_start"]), row["lifecycle_revision"])

    def register(self, registration: Registration, verified_binding: VerifiedBinding, *, native_runner=False, rebind_deck_id=None,
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
            if rebind_deck_id:
                target = db.execute("SELECT * FROM sessions WHERE deck_id=? AND retired=0", (rebind_deck_id,)).fetchone()
                if not target or (target["workspace"], target["directory"], target["session_id"]) != (registration.workspace, directory, conversation.id):
                    raise DeckError("binding_mismatch", "Cannot rebind another conversation", 4)
                db.execute("""UPDATE sessions SET endpoint=?,socket=?,generation=?,pane_id=?,registration=?,binding=?,api_directory=?,
                    archived=0,native_runner=?,cached_title=?,last_activity=? WHERE deck_id=?""", (
                    endpoint, pane.socket_path, pane.generation, pane.pane_id, json.dumps(asdict(registration)), json.dumps(asdict(pane)),
                    conversation.directory, native_runner, conversation.title or registration.title, conversation.updated_ms / 1000, rebind_deck_id))
                return self.decode(db.execute("SELECT * FROM sessions WHERE deck_id=?", (rebind_deck_id,)).fetchone())
            deck_id = str(uuid.uuid4())
            db.execute("""INSERT INTO sessions
                (deck_id,workspace,directory,endpoint,session_id,socket,generation,pane_id,
                 registration,binding,api_directory,created_at,last_activity,cached_title,native_runner,restore_on_start,lifecycle_revision)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    deck_id, registration.workspace, directory, endpoint, conversation.id,
                    pane.socket_path, pane.generation, pane.pane_id, json.dumps(asdict(registration)),
                    json.dumps(asdict(pane)), conversation.directory, now, conversation.updated_ms / 1000,
                    conversation.title or registration.title, native_runner, native_runner, 0))
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

    def claim_start(self, deck_id: str, *, manual_resume=False) -> LifecycleClaim:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE deck_id=?", (deck_id,)).fetchone()
            if not row or row["retired"]:
                raise DeckError("not_found", "Unknown SessionDeck ID", 2)
            revision = row["lifecycle_revision"]
            if manual_resume and not row["restore_on_start"]:
                revision += 1
                db.execute("UPDATE sessions SET restore_on_start=1,archived=0,lifecycle_revision=? WHERE deck_id=?", (revision, deck_id))
            elif not row["restore_on_start"]:
                raise DeckError("not_restorable", "Session was explicitly unloaded", 4)
            if db.execute("SELECT 1 FROM lifecycle_claims WHERE deck_id=?", (deck_id,)).fetchone():
                raise DeckError("start_in_progress", "Session startup is already in progress", 4)
            token = str(uuid.uuid4())
            db.execute("INSERT INTO lifecycle_claims VALUES (?,?,?)", (deck_id, token, revision))
            return LifecycleClaim(token, deck_id, revision)

    def validate_claim(self, claim: LifecycleClaim) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT revision FROM lifecycle_claims WHERE deck_id=? AND token=?", (claim.deck_id, claim.token)).fetchone()
            session = db.execute("SELECT restore_on_start,lifecycle_revision FROM sessions WHERE deck_id=?", (claim.deck_id,)).fetchone()
            return bool(row and session and session["restore_on_start"] and row["revision"] == claim.revision == session["lifecycle_revision"])

    def finish_claim(self, claim: LifecycleClaim):
        with self.connect() as db:
            db.execute("DELETE FROM lifecycle_claims WHERE deck_id=? AND token=?", (claim.deck_id, claim.token))

    def begin_unload(self, deck_id: str, expected_members: tuple[str, ...]) -> UnloadGroup:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE deck_id=? AND retired=0", (deck_id,)).fetchone()
            if not row:
                raise DeckError("not_found", "Unknown SessionDeck ID", 2)
            if not row["native_runner"]:
                raise DeckError("external_session", "External sessions cannot be unloaded by SessionDeck", 4)
            key = (row["socket"], row["generation"], row["pane_id"])
            members = tuple(item[0] for item in db.execute("SELECT deck_id FROM sessions WHERE retired=0 AND socket=? AND generation=? AND pane_id=? ORDER BY deck_id", key))
            if tuple(sorted(expected_members)) != members:
                raise DeckError("membership_changed", "Session membership changed; confirm again", 4)
            db.execute("INSERT OR IGNORE INTO closing_bindings VALUES (?,?,?)", key)
            for member in members:
                db.execute("UPDATE sessions SET restore_on_start=0,archived=1,lifecycle_revision=lifecycle_revision+1 WHERE deck_id=?", (member,))
                db.execute("DELETE FROM lifecycle_claims WHERE deck_id=?", (member,))
            return UnloadGroup(PaneBinding(**json.loads(row["binding"])), members)

    def binding_members(self, deck_id: str) -> tuple[str, ...]:
        with self.connect() as db:
            row = db.execute("SELECT socket,generation,pane_id FROM sessions WHERE deck_id=? AND retired=0", (deck_id,)).fetchone()
            if not row:
                raise DeckError("not_found", "Unknown SessionDeck ID", 2)
            return tuple(item[0] for item in db.execute(
                "SELECT deck_id FROM sessions WHERE retired=0 AND socket=? AND generation=? AND pane_id=? ORDER BY deck_id",
                tuple(row)))

    def logical_session(self, workspace: str, directory: str, session_id: str) -> ManagedSession | None:
        directory = str(Path(directory).expanduser().resolve())
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE retired=0 AND workspace=? AND directory=? AND session_id=? ORDER BY created_at DESC LIMIT 1",
                             (workspace, directory, session_id)).fetchone()
            return self.decode(row) if row else None

    def request_recovery(self):
        with self.connect() as db:
            db.execute("UPDATE recovery_requests SET requested=1 WHERE id=1")

    def consume_recovery_request(self) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            requested = bool(db.execute("SELECT requested FROM recovery_requests WHERE id=1").fetchone()[0])
            db.execute("UPDATE recovery_requests SET requested=0 WHERE id=1")
            return requested

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
            matches = []
            for row in db.execute("SELECT data FROM launches ORDER BY rowid"):
                launch = json.loads(row[0])
                if launch.get("deck_id") == deck_id:
                    matches.append(launch)
                    continue
                if launch.get("deck_id"):
                    owner = db.execute("SELECT * FROM sessions WHERE deck_id=?", (launch["deck_id"],)).fetchone()
                    if owner and (owner["endpoint"], owner["socket"], owner["generation"], owner["pane_id"]) == (
                            target.registration.endpoint, target.binding.socket_path, target.binding.generation, target.binding.pane_id):
                        matches.append(launch)
            return next((launch for launch in reversed(matches) if launch.get("state") not in {"stopped", "failed"}),
                        matches[-1] if matches else None)

    def audit_attempt(self, ref, decision: str) -> str:
        audit_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (audit_id, json.dumps(asdict(ref)), decision, "attempted", time.time()))
        return audit_id

    def audit_outcome(self, audit_id: str, outcome: str):
        with self.connect() as db:
            db.execute("UPDATE audit SET outcome=? WHERE id=?", (outcome, audit_id))
