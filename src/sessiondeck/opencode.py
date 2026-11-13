import asyncio
import re
import time
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from .events import parse_sse
from .models import (
    Conversation, DeckError, Endpoint, PendingPermission, PendingQuestion,
    PermissionRef, SessionSnapshot,
)

SUPPORTED_VERSION = "1.19.2"


def normalize_endpoint(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parts.username is not None or parts.password is not None
                or parts.path not in {"", "/"} or parts.query or parts.fragment or not parts.port):
            raise ValueError
        host = "[::1]" if parts.hostname == "::1" else "127.0.0.1"
        return f"http://{host}:{parts.port}"
    except ValueError:
        raise DeckError("invalid_endpoint", "Expected a loopback HTTP URL with an explicit port", 2) from None


class OpenCodeClient:
    def __init__(self, endpoint: Endpoint, *, auth=None, transport=None):
        self.endpoint = normalize_endpoint(endpoint.url)
        self.http = httpx.AsyncClient(
            base_url=self.endpoint, auth=auth, transport=transport,
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(5, connect=2),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def request(self, method: str, path: str, *, directory=None, body=None):
        try:
            response = await self.http.request(
                method, path, params={"directory": directory} if directory is not None else None,
                json=body,
            )
            if response.status_code == 404:
                raise DeckError("not_found", "OpenCode resource no longer exists")
            if response.status_code in {401, 403}:
                raise DeckError("unauthorized", "OpenCode authentication failed")
            if response.is_error or response.is_redirect:
                raise DeckError("api_error", f"OpenCode returned HTTP {response.status_code}")
            return response.json()
        except httpx.HTTPError:
            raise DeckError("unreachable", "OpenCode request failed or timed out") from None
        except ValueError:
            raise DeckError("invalid_response", "OpenCode returned invalid JSON") from None

    async def health(self) -> dict:
        info = await self.request("GET", "/global/health")
        if not isinstance(info, dict) or info.get("healthy") is not True:
            raise DeckError("invalid_response", "Invalid OpenCode health response")
        if info.get("version") != SUPPORTED_VERSION:
            raise DeckError("unsupported_version", f"OpenCode {SUPPORTED_VERSION} is required")
        return info

    @staticmethod
    def conversation(info) -> Conversation:
        try:
            if not isinstance(info["id"], str) or not isinstance(info["directory"], str) or not isinstance(info["title"], str):
                raise ValueError
            return Conversation(info["id"], info["directory"], info["title"], int(info["time"]["updated"]))
        except (KeyError, TypeError, ValueError):
            raise DeckError("invalid_response", "Invalid OpenCode conversation") from None

    async def get_session(self, directory: str, session_id: str) -> Conversation:
        return self.conversation(await self.request("GET", f"/session/{session_id}", directory=directory))

    async def create_session(self, directory: str, title: str | None = None) -> Conversation:
        return self.conversation(await self.request("POST", "/session", directory=directory,
                                                    body={"title": title} if title else {}))

    async def snapshot(self, directory: str, session_id: str) -> SessionSnapshot:
        await self.health()
        try:
            session = await self.get_session(directory, session_id)
        except DeckError as exc:
            if exc.code == "not_found":
                raise DeckError("session_missing", "Registered OpenCode conversation was deleted") from None
            raise
        status, permissions, questions = await asyncio.gather(
            self.request("GET", "/session/status", directory=directory),
            self.request("GET", "/permission", directory=directory),
            self.request("GET", "/question", directory=directory),
        )
        try:
            if not isinstance(status, dict) or not isinstance(permissions, list) or not isinstance(questions, list):
                raise ValueError
            execution = status.get(session_id, {"type": "idle"})
            if execution["type"] not in {"idle", "busy", "retry"}:
                raise ValueError
            for item in permissions:
                if (not isinstance(item["id"], str) or not re.fullmatch(r"per[A-Za-z0-9_-]+", item["id"])
                        or not isinstance(item["sessionID"], str) or not isinstance(item["permission"], str)
                        or not isinstance(item["patterns"], list) or not all(isinstance(p, str) for p in item["patterns"])):
                    raise ValueError
            for item in questions:
                if (not isinstance(item["id"], str) or not isinstance(item["sessionID"], str)
                        or not isinstance(item["questions"], list)
                        or not all(isinstance(q["question"], str) for q in item["questions"])):
                    raise ValueError
            pending = tuple(PendingPermission(
                PermissionRef(self.endpoint, directory, session_id, item["id"]),
                item["permission"], tuple(item["patterns"]),
            ) for item in permissions if item["sessionID"] == session_id)
            asking = tuple(PendingQuestion(item["id"], tuple(q["question"] for q in item["questions"]))
                           for item in questions if item["sessionID"] == session_id)
            return SessionSnapshot(session, execution["type"], pending, asking, time.time(), execution.get("message", ""))
        except (KeyError, TypeError, ValueError):
            raise DeckError("invalid_response", "Invalid OpenCode status/attention response") from None

    async def reply(self, ref: PermissionRef, decision: str) -> bool:
        if (decision not in {"once", "reject"} or normalize_endpoint(ref.endpoint) != self.endpoint
                or not isinstance(ref.request_id, str) or not re.fullmatch(r"per[A-Za-z0-9_-]+", ref.request_id)):
            raise DeckError("invalid_decision", "Invalid permission decision or endpoint", 2)
        result = await self.request("POST", f"/permission/{ref.request_id}/reply",
                                    directory=ref.directory, body={"reply": decision})
        if not isinstance(result, bool):
            raise DeckError("invalid_response", "Invalid permission reply result")
        return result

    async def select_session(self, directory: str, session_id: str) -> bool:
        result = await self.request("POST", "/tui/select-session", directory=directory, body={"sessionID": session_id})
        if result is not True:
            raise DeckError("selection_failed", "OpenCode did not accept session selection")
        return True

    async def events(self, directory: str) -> AsyncIterator[dict]:
        try:
            async with self.http.stream("GET", "/event", params={"directory": directory},
                                        timeout=httpx.Timeout(30, connect=2)) as response:
                response.raise_for_status()
                async for event in parse_sse(response.aiter_lines()):
                    yield event
        except httpx.HTTPError:
            raise DeckError("event_disconnected", "OpenCode event stream disconnected") from None
