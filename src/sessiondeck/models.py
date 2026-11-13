from dataclasses import dataclass, field
from typing import Any


class DeckError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 3):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class Endpoint:
    url: str
    credential_ref: str | None = None


@dataclass(frozen=True)
class Conversation:
    id: str
    directory: str
    title: str
    updated_ms: int


@dataclass(frozen=True)
class PermissionRef:
    endpoint: str
    directory: str
    session_id: str
    request_id: str


@dataclass(frozen=True)
class PendingPermission:
    ref: PermissionRef
    action: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class PendingQuestion:
    request_id: str
    questions: tuple[str, ...]


@dataclass(frozen=True)
class SessionSnapshot:
    conversation: Conversation
    execution: str
    permissions: tuple[PendingPermission, ...]
    questions: tuple[PendingQuestion, ...]
    checked_at: float
    retry_message: str = ""


@dataclass(frozen=True)
class PaneBinding:
    socket_path: str
    generation: str
    session_id: str
    window_id: str
    pane_id: str
    dead: bool = False


@dataclass(frozen=True)
class RuntimeIdentity:
    socket_path: str
    generation: str
    session_id: str
    dashboard_pane_id: str


@dataclass(frozen=True)
class Registration:
    workspace: str
    directory: str
    endpoint: str
    opencode_session_id: str
    tmux_socket: str
    tmux_generation: str
    tmux_pane_id: str
    title: str = ""
    reference_url: str = ""
    credential_ref: str | None = None


@dataclass(frozen=True)
class VerifiedBinding:
    pane: PaneBinding
    conversation: Conversation


@dataclass(frozen=True)
class ManagedSession:
    deck_id: str
    registration: Registration
    api_directory: str
    binding: PaneBinding
    archived: bool
    retired: bool
    created_at: float
    last_activity: float
    native_runner: bool = True
    restore_on_start: bool = True
    lifecycle_revision: int = 0


@dataclass(frozen=True)
class LifecycleClaim:
    token: str
    deck_id: str
    revision: int


@dataclass(frozen=True)
class UnloadGroup:
    binding: PaneBinding
    deck_ids: tuple[str, ...]


@dataclass(frozen=True)
class SessionView:
    session: ManagedSession
    status: str
    title: str
    last_activity: float
    detail: str = ""
    stale: bool = False
    permissions: tuple[PendingPermission, ...] = ()
    questions: tuple[PendingQuestion, ...] = ()


@dataclass(frozen=True)
class DashboardSnapshot:
    sessions: dict[str, SessionView] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionResult:
    outcome: str
    message: str = ""


JsonObject = dict[str, Any]
