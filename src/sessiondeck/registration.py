import asyncio
import os
import re
from dataclasses import replace
from pathlib import Path

from .models import DeckError, Endpoint, Registration, VerifiedBinding
from .opencode import OpenCodeClient, normalize_endpoint
from .registry import Registry
from .tmux import TmuxRuntime


async def register_session(registry: Registry, runtime: TmuxRuntime, registration: Registration):
    if not re.fullmatch(r"ses[A-Za-z0-9_-]+", registration.opencode_session_id):
        raise DeckError("invalid_session", "Invalid OpenCode session ID", 2)
    try:
        directory = Path(registration.directory).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise OSError
    except (OSError, RuntimeError):
        raise DeckError("invalid_directory", "Project directory does not exist", 2) from None
    registration = replace(registration, directory=str(directory), endpoint=normalize_endpoint(registration.endpoint))
    pane = await asyncio.to_thread(runtime.inspect, registration.tmux_pane_id, registration.tmux_generation)
    if pane.socket_path != str(Path(registration.tmux_socket).resolve()) or pane.dead:
        raise DeckError("binding_mismatch", "Pane does not belong to this live runtime", 4)
    async with OpenCodeClient(Endpoint(registration.endpoint), auth=registry.state.auth(registration.credential_ref)) as client:
        await client.health()
        conversation = await client.get_session(registration.directory, registration.opencode_session_id)
    known = registry.list(include_archived=True)
    present = await asyncio.to_thread(runtime.panes)
    again = await asyncio.to_thread(runtime.inspect, registration.tmux_pane_id, registration.tmux_generation)
    if again != pane or present.get(pane.pane_id) != pane:
        raise DeckError("binding_changed", "Pane changed while validating registration", 4)
    obsolete = {record.deck_id: record.binding for record in known
                if record.binding.socket_path == pane.socket_path
                and (record.binding.generation != pane.generation or record.binding.pane_id not in present)}
    registration = replace(registration, endpoint=normalize_endpoint(registration.endpoint))
    return registry.register(registration, VerifiedBinding(pane, conversation), obsolete_bindings=obsolete)


async def register_input(registry: Registry, runtime: TmuxRuntime, data: dict):
    data = dict(data)
    auth_env, username = data.pop("auth_env", None), data.pop("username", "opencode")
    if not isinstance(username, str):
        raise DeckError("invalid_input", "username must be a string", 2)
    if "credential_ref" in data:
        raise DeckError("invalid_input", "credential_ref is not an external registration field", 2)
    credential = None
    if auth_env is not None:
        if not isinstance(auth_env, str) or not os.environ.get(auth_env):
            raise DeckError("missing_credential", "Requested auth environment variable is unavailable", 2)
        credential = registry.state.save_credential(username, os.environ[auth_env])
    try:
        registration = Registration(**data, credential_ref=credential)
        if any(not isinstance(value, str) for value in data.values()):
            raise TypeError
        return await register_session(registry, runtime, registration)
    except TypeError:
        raise DeckError("invalid_input", "Invalid registration fields", 2) from None
