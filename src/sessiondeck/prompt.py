import re

from .models import DeckError, Endpoint
from .opencode import OpenCodeClient


async def send_prompt(registry, runtime, session_id: str, text: str) -> dict:
    if not re.fullmatch(r"ses[A-Za-z0-9_-]+", session_id) or not isinstance(text, str) or not text.strip():
        raise DeckError("invalid_input", "Provide an OpenCode session ID and non-empty text", 2)
    session = next((item for item in registry.list() if item.registration.opencode_session_id == session_id), None)
    if session is None:
        raise DeckError("not_managed", "OpenCode session is not managed by SessionDeck", 4)
    pane = runtime.inspect(session.binding.pane_id, session.binding.generation)
    if pane.dead or pane.socket_path != session.binding.socket_path:
        raise DeckError("session_stopped", "Managed session is not running", 4)
    async with OpenCodeClient(Endpoint(session.registration.endpoint), auth=registry.state.auth(session.registration.credential_ref)) as client:
        await client.get_session(session.api_directory, session_id)
        await client.prompt(session.api_directory, session_id, text)
    return {"deck_id": session.deck_id, "session_id": session_id, "state": "queued"}
