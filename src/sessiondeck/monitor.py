import asyncio
import time
from dataclasses import replace
from collections.abc import AsyncIterator

from .models import (
    DashboardSnapshot, DecisionResult, DeckError, Endpoint, ManagedSession,
    PermissionRef, SessionView,
)
from .opencode import OpenCodeClient
from .registry import Registry
from .tmux import TmuxRuntime

REFRESH_INTERVAL = 5
MAX_BACKOFF = 30
MIN_REFRESH_SPACING = 1


class Monitor:
    def __init__(self, registry: Registry, runtime: TmuxRuntime, client_factory=None):
        self.registry = registry
        self.runtime = runtime
        self.client_factory = client_factory or self._client
        self.snapshot = DashboardSnapshot()
        self.dirty = asyncio.Event()
        self.registry_refresh_requested = False
        self.decision_locks: dict[PermissionRef, asyncio.Lock] = {}

    def _client(self, registration):
        return OpenCodeClient(Endpoint(registration.endpoint), auth=self.registry.state.auth(registration.credential_ref))

    async def _refresh_session(self, session: ManagedSession) -> SessionView:
        session = self.registry.get(session.deck_id)
        title = self.registry.cached_title(session.deck_id) or session.registration.title
        activity = session.last_activity
        launch = self.registry.launch_for(session.deck_id)
        if launch and launch["state"] in {"stopped", "failed"}:
            return SessionView(session, "stopped", title, activity, "Native session has finished")
        try:
            pane = await asyncio.to_thread(self.runtime.inspect, session.binding.pane_id, session.binding.generation)
            if pane.dead or pane.socket_path != session.binding.socket_path:
                return SessionView(session, "stopped", title, activity, "Registered pane is no longer live")
            async with self.client_factory(session.registration) as client:
                current = await client.snapshot(session.api_directory, session.registration.opencode_session_id)
            title = current.conversation.title or title
            activity = current.conversation.updated_ms / 1000
            await asyncio.to_thread(self.registry.cache, session.deck_id, title, activity)
            if current.permissions:
                status, detail = "needs approval", current.permissions[0].action
            elif current.questions:
                questions = current.questions[0].questions
                status, detail = "waiting", "Question: " + (questions[0] if questions else "Answer in OpenCode")
            elif current.execution == "retry":
                status, detail = "waiting", current.retry_message
            elif current.execution == "busy":
                status, detail = "working", "Agent is working"
            else:
                status, detail = "idle", "Ready for a message"
            if launch and launch["state"] == "tui_lost":
                detail = "TUI disconnected — Enter to recover. " + detail
            return SessionView(session, status, title, activity, detail, permissions=current.permissions, questions=current.questions)
        except DeckError as exc:
            if exc.code in {"pane_missing", "stale_runtime", "session_missing"}:
                return SessionView(session, "stopped", title, activity, str(exc))
            return SessionView(session, "unreachable", title, activity, str(exc), stale=True)

    async def refresh(self) -> DashboardSnapshot:
        sessions = self.registry.list()
        views = await asyncio.gather(*(self._refresh_session(session) for session in sessions))
        self.snapshot = DashboardSnapshot({view.session.deck_id: view for view in views})
        return self.snapshot

    def request_registry_refresh(self):
        self.registry_refresh_requested = True
        self.dirty.set()

    async def archive_completed(self, deck_ids: tuple[str, ...]) -> int:
        archived = 0
        for deck_id in dict.fromkeys(deck_ids):
            session = self.registry.get(deck_id)
            if session.archived or session.retired:
                continue
            current = await self._refresh_session(session)
            if current.status == "stopped":
                await asyncio.to_thread(self.registry.set_archived, deck_id, True)
                archived += 1
        self.request_registry_refresh()
        return archived

    async def _stream(self, registration, directory):
        backoff = 1
        while True:
            try:
                async with self.client_factory(registration) as client:
                    async for event in client.events(directory):
                        if event.get("type"):
                            self.dirty.set()
                            backoff = 1
            except DeckError:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def watch(self) -> AsyncIterator[DashboardSnapshot]:
        streams, workers = {}, {}
        stream_credentials = {}
        updates = asyncio.Queue()
        values = dict(self.snapshot.sessions)
        queue_waiter = asyncio.create_task(updates.get())
        dirty_waiter = asyncio.create_task(self.dirty.wait())
        next_scan = 0

        async def follow(session, token, wake):
            while True:
                wake.clear()
                started = time.monotonic()
                previous = self.snapshot.sessions.get(session.deck_id)
                if previous is not None:
                    await updates.put((session.deck_id, token, replace(previous, stale=True)))
                view = await self._refresh_session(session)
                await updates.put((session.deck_id, token, view))
                await asyncio.sleep(max(0, MIN_REFRESH_SPACING - (time.monotonic() - started)))
                try:
                    await asyncio.wait_for(wake.wait(), max(.05, REFRESH_INTERVAL - (time.monotonic() - started)))
                except TimeoutError:
                    pass

        try:
            while True:
                now = time.monotonic()
                if now >= next_scan:
                    sessions = {s.deck_id: s for s in self.registry.list()}
                    groups = {(s.registration.endpoint, s.api_directory): s.registration for s in sessions.values()}
                    cancelled = []
                    for key in list(streams):
                        if key not in groups or stream_credentials[key] != groups[key].credential_ref:
                            cancelled.append(streams.pop(key))
                            stream_credentials.pop(key)
                    for key in groups.keys() - streams.keys():
                        streams[key] = asyncio.create_task(self._stream(groups[key], key[1]))
                        stream_credentials[key] = groups[key].credential_ref
                    for key in list(workers):
                        session = sessions.get(key)
                        signature = (session.registration, session.api_directory, session.binding) if session else None
                        if signature != workers[key][1]:
                            cancelled.append(workers.pop(key)[3])
                            values.pop(key, None)
                    for session in sessions.values():
                        if session.deck_id not in workers:
                            token, wake = object(), asyncio.Event()
                            signature = (session.registration, session.api_directory, session.binding)
                            workers[session.deck_id] = (token, signature, wake, asyncio.create_task(follow(session, token, wake)))
                            values[session.deck_id] = SessionView(session, "checking", self.registry.cached_title(session.deck_id),
                                                                  session.last_activity, "Checking OpenCode…", stale=True)
                    for task in cancelled:
                        task.cancel()
                    await asyncio.gather(*cancelled, return_exceptions=True)
                    next_scan = now + REFRESH_INTERVAL
                    self.snapshot = DashboardSnapshot(dict(values))
                    yield self.snapshot
                done, _ = await asyncio.wait((queue_waiter, dirty_waiter), timeout=max(0, next_scan - time.monotonic()),
                                              return_when=asyncio.FIRST_COMPLETED)
                if queue_waiter in done:
                    key, token, view = queue_waiter.result()
                    queue_waiter = asyncio.create_task(updates.get())
                    if key in workers and workers[key][0] is token:
                        values[key] = view
                        self.snapshot = DashboardSnapshot(dict(values))
                        yield self.snapshot
                if dirty_waiter in done:
                    self.dirty.clear()
                    dirty_waiter = asyncio.create_task(self.dirty.wait())
                    if self.registry_refresh_requested:
                        self.registry_refresh_requested = False
                        next_scan = 0
                    for _, _, wake, _ in workers.values():
                        wake.set()
        finally:
            tasks = [*streams.values(), *(worker[3] for worker in workers.values()), queue_waiter, dirty_waiter]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def decide(self, ref: PermissionRef, decision: str) -> DecisionResult:
        if decision not in {"once", "reject"}:
            return DecisionResult("failed", "Only once or reject is supported")
        async with self.decision_locks.setdefault(ref, asyncio.Lock()):
            candidates = [s for s in self.registry.list() if
                          (s.registration.endpoint, s.api_directory, s.registration.opencode_session_id) ==
                          (ref.endpoint, ref.directory, ref.session_id)]
            if not candidates:
                return DecisionResult("failed", "Request is no longer managed")
            audit_id = None
            try:
                for session in candidates:
                    try:
                        pane = await asyncio.to_thread(self.runtime.inspect, session.binding.pane_id, session.binding.generation)
                    except DeckError as exc:
                        if exc.code in {"pane_missing", "stale_runtime"}:
                            continue
                        raise
                    if not pane.dead and pane.socket_path == session.binding.socket_path and pane.generation == session.binding.generation:
                        break
                else:
                    return DecisionResult("failed", "No live agent pane owns this request")
                async with self.client_factory(session.registration) as client:
                    current = await client.snapshot(ref.directory, ref.session_id)
                    if ref not in {permission.ref for permission in current.permissions}:
                        return DecisionResult("already_handled", "Request no longer pending")
                    audit_id = self.registry.audit_attempt(ref, decision)
                    result = await client.reply(ref, decision)
                outcome = "confirmed" if result else "failed"
                self.registry.audit_outcome(audit_id, outcome)
                self.dirty.set()
                return DecisionResult(outcome, "Decision sent" if result else "OpenCode rejected the decision")
            except DeckError as exc:
                outcome = "unknown" if audit_id else "failed"
                if audit_id:
                    self.registry.audit_outcome(audit_id, outcome)
                self.dirty.set()
                return DecisionResult(outcome, str(exc))
            except asyncio.CancelledError:
                if audit_id:
                    self.registry.audit_outcome(audit_id, "unknown")
                raise

    async def attach(self, deck_id: str):
        session = self.registry.get(deck_id)
        if session.retired or session.archived:
            raise DeckError("not_managed", "Session is archived or unregistered", 2)
        pane = await asyncio.to_thread(self.runtime.inspect, session.binding.pane_id, session.binding.generation)
        if pane.dead:
            raise DeckError("pane_missing", "Agent pane is stopped")
        launch = self.registry.launch_for(deck_id)
        if launch and launch["state"] == "tui_lost":
            self.registry.update_launch(launch["id"], restart_tui=True,
                                         restart_session_id=session.registration.opencode_session_id)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.registry.launch(launch["id"])["state"] == "ready":
                    break
                await asyncio.sleep(.1)
            else:
                raise DeckError("recovery_failed", "TUI did not recover")
        async with self.client_factory(session.registration) as client:
            await client.health()
            await client.get_session(session.api_directory, session.registration.opencode_session_id)
            await client.select_session(session.api_directory, session.registration.opencode_session_id)
        await asyncio.to_thread(self.runtime.focus, session.binding)
