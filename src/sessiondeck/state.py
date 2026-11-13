import json
import os
import re
import secrets
from pathlib import Path

from .models import DeckError


class State:
    def __init__(self, root: Path | str | None = None):
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.root = Path(root or os.environ.get("SESSIONDECK_STATE_DIR", base / "opencode-sessiondeck")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def save_credential(self, username: str, password: str) -> str:
        directory = self.root / "credentials"
        directory.mkdir(exist_ok=True, mode=0o700)
        name = secrets.token_hex(16) + ".json"
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"username": username, "password": password}, stream)
        return name

    def auth(self, reference: str | None):
        if reference is None:
            return None
        if not re.fullmatch(r"[a-f0-9]{32}\.json", reference):
            raise DeckError("invalid_credential", "Invalid local credential reference", 2)
        try:
            data = json.loads((self.root / "credentials" / reference).read_text())
            return data["username"], data["password"]
        except (OSError, ValueError, KeyError, TypeError):
            raise DeckError("missing_credential", "Cannot load local OpenCode credential") from None
