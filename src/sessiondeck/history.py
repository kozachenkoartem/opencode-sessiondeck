import asyncio
import os
import re
import secrets
import signal
import sys
import tempfile
from pathlib import Path

from .models import Conversation, DeckError, Endpoint
from .opencode import OpenCodeClient

METADATA_TIMEOUT = 20
LOOKUP_SHUTDOWN_TIMEOUT = 3
READINESS_BUFFER_SIZE = 1024
READ_CHUNK_SIZE = 4096
SHUTDOWN_POLL_INTERVAL = .05


async def _stop_metadata_server(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = asyncio.get_running_loop().time() + LOOKUP_SHUTDOWN_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), LOOKUP_SHUTDOWN_TIMEOUT)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), LOOKUP_SHUTDOWN_TIMEOUT)


async def _read_metadata(session_id: str) -> Conversation:
    password = secrets.token_urlsafe(32)
    env = os.environ | {"OPENCODE_PURE": "1", "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                        "OPENCODE_SERVER_USERNAME": "opencode", "OPENCODE_SERVER_PASSWORD": password}
    env.pop("TMUX", None)
    process = drainer = None
    with tempfile.TemporaryDirectory(prefix="sessiondeck-lookup-") as directory:
        try:
            async with asyncio.timeout(METADATA_TIMEOUT):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "sessiondeck", "_backend", str(os.getpid()),
                    env=env, cwd=directory, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                ready = asyncio.get_running_loop().create_future()

                async def drain():
                    buffer = b""
                    while chunk := await process.stdout.read(READ_CHUNK_SIZE):
                        combined = buffer + chunk
                        match = re.search(rb"opencode server listening on (http://127\.0\.0\.1:\d+)[\r\n ]", combined)
                        if match and not ready.done():
                            ready.set_result(match[1].decode())
                        buffer = combined[-READINESS_BUFFER_SIZE:]
                    if not ready.done():
                        ready.set_exception(DeckError("session_lookup_failed", "Metadata server exited before readiness"))

                drainer = asyncio.create_task(drain())
                endpoint = await ready
                async with OpenCodeClient(Endpoint(endpoint), auth=("opencode", password)) as client:
                    await client.health()
                    return await client.get_session(directory, session_id)
        finally:
            try:
                if process is not None:
                    await _stop_metadata_server(process)
            finally:
                if drainer is not None:
                    drainer.cancel()
                    await asyncio.gather(drainer, return_exceptions=True)


def lookup_session_directory(session_id: str) -> str:
    if not isinstance(session_id, str) or not re.fullmatch(r"ses[A-Za-z0-9_-]+", session_id):
        raise DeckError("invalid_session", "Invalid OpenCode session ID", 2)
    try:
        info = asyncio.run(_read_metadata(session_id))
    except DeckError as exc:
        if exc.code == "not_found":
            raise DeckError("session_not_found", "OpenCode conversation not found in the current data environment", 2) from None
        if exc.code in {"unsupported_version", "invalid_response"}:
            raise
        raise DeckError("session_lookup_failed", "Cannot read session metadata; supply --directory explicitly") from None
    except (OSError, TimeoutError):
        raise DeckError("session_lookup_failed", "Cannot read session metadata; supply --directory explicitly") from None
    directory = info.directory
    if (info.id != session_id or not isinstance(directory, str) or "\x00" in directory
            or not Path(directory).is_absolute()):
        raise DeckError("invalid_session_metadata", "OpenCode returned invalid session directory metadata")
    return directory
