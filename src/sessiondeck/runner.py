import asyncio
import fcntl
import hashlib
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

from .history import lookup_session_directory
from .models import DeckError, Endpoint, Registration
from .opencode import OpenCodeClient
from .registration import register_session
from .registry import Registry
from .tmux import TmuxRuntime

START_TIMEOUT = 30
POLL_INTERVAL = .15
LOG_LIMIT = 1024 * 1024
CLEANUP_GRACE = 3


def _assert_conversation_available(registry, runtime, directory, session_id, socket_path):
    if not session_id:
        return
    for existing in registry.list(directory=directory, include_archived=True):
        if existing.registration.opencode_session_id != session_id:
            continue
        if existing.binding.socket_path == socket_path:
            try:
                runtime.inspect(existing.binding.pane_id, existing.binding.generation)
            except DeckError as exc:
                if exc.code in {"pane_missing", "stale_runtime"}:
                    continue
                raise
        raise DeckError("already_registered", "Conversation is already managed; open it or unregister its old binding before resuming", 4)


def _acquire_resume_lock(state, directory, session_id, stopping):
    if not session_id:
        return None
    locks = state.root / "resume-locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    key = hashlib.sha256(os.fsencode(directory) + b"\x00" + session_id.encode()).hexdigest()
    fd = os.open(locks / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while not stopping.is_set():
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                stopping.wait(POLL_INTERVAL)
        raise DeckError("launch_cancelled", "Launch cancelled while waiting for another startup")
    except BaseException:
        os.close(fd)
        raise


def _release_resume_lock(fd):
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def start_session(registry: Registry, runtime: TmuxRuntime, workspace: str, directory: str | None = None,
                  session_id=None, title=None, reference_url=None) -> dict:
    if sys.platform != "linux":
        raise DeckError("unsupported_platform", "Native launch currently requires Linux")
    if workspace not in {"work", "personal"}:
        raise DeckError("invalid_workspace", "Workspace must be work or personal", 2)
    if directory is None:
        if not session_id:
            raise DeckError("invalid_input", "Provide --directory for a new session, or --session to resume", 2)
        directory = lookup_session_directory(session_id)
    try:
        directory = str(Path(directory).expanduser().resolve(strict=True))
        if not Path(directory).is_dir():
            raise OSError
    except OSError:
        raise DeckError("invalid_directory", "Project directory does not exist", 2) from None
    identity = runtime.ensure()
    rebind_deck_id = None
    if session_id and (existing := registry.logical_session(workspace, directory, session_id)):
        claim = registry.claim_start(existing.deck_id, manual_resume=True)
        registry.finish_claim(claim)
        rebind_deck_id = existing.deck_id
    _assert_conversation_available(registry, runtime, directory, session_id, identity.socket_path)
    credential = registry.state.save_credential("opencode", secrets.token_urlsafe(32))
    attempt = registry.create_launch({"workspace": workspace, "directory": directory,
                                      "session_id": session_id, "title": title, "reference_url": reference_url,
                                       "credential_ref": credential, "generation": identity.generation, "rebind_deck_id": rebind_deck_id})
    try:
        pane = runtime.new_window(directory, [sys.executable, "-m", "sessiondeck", "_run", attempt["id"]])
        registry.update_launch(attempt["id"], pane_id=pane.pane_id)
    except DeckError:
        registry.update_launch(attempt["id"], state="failed", error="Cannot create runner window")
        raise
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        current = registry.launch(attempt["id"])
        if current["state"] in {"ready", "tui_lost"}:
            return current
        if current["state"] in {"failed", "stopped"}:
            raise DeckError("launch_failed", f"Launch {attempt['id']}: " + current.get("error", "OpenCode exited before readiness"))
        try:
            runtime.inspect(pane.pane_id, pane.generation)
        except DeckError as exc:
            if exc.code in {"pane_missing", "runtime_missing", "stale_runtime"}:
                registry.update_launch(attempt["id"], state="failed", error="Runner disappeared during startup")
                raise DeckError("launch_failed", f"Launch {attempt['id']}: runner disappeared during startup") from None
        time.sleep(POLL_INTERVAL)
    return registry.launch(attempt["id"])


def terminate_process(process, group=False):
    if process is None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + CLEANUP_GRACE
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(POLL_INTERVAL)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        elif process.poll() is None:
            process.terminate()
        process.wait(timeout=CLEANUP_GRACE)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            if group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def run_attempt(registry: Registry, runtime: TmuxRuntime, launch_id: str) -> int:
    attempt = registry.launch(launch_id)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    for sig in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(sig, lambda *_: None)
    os.umask(0o077)
    backend = tui = None
    resume_lock = None
    terminal = termios.tcgetattr(0) if os.isatty(0) else None
    env = os.environ.copy()
    username, password = registry.state.auth(attempt["credential_ref"])
    env.update(OPENCODE_SERVER_USERNAME=username, OPENCODE_SERVER_PASSWORD=password)
    env.pop("OPENCODE_SERVER_URL", None)
    registry.update_launch(launch_id, runner_pid=os.getpid(), runner_pgrp=os.getpgrp())
    try:
        if attempt["session_id"]:
            registry.update_launch(launch_id, waiting_for_resume_lock=True)
            resume_lock = _acquire_resume_lock(registry.state, attempt["directory"], attempt["session_id"], stopping)
            registry.update_launch(launch_id, waiting_for_resume_lock=False)
        if stopping.is_set():
            raise DeckError("launch_cancelled", "Launch cancelled before backend startup")
        pane = runtime.inspect(os.environ["TMUX_PANE"], attempt["generation"])
        _assert_conversation_available(registry, runtime, attempt["directory"], attempt["session_id"], pane.socket_path)
        backend = subprocess.Popen([sys.executable, "-m", "sessiondeck", "_backend", str(os.getpid())],
                                    cwd=attempt["directory"], env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        registry.update_launch(launch_id, backend_pid=backend.pid)
        announcement = queue.Queue()
        logdir = registry.state.root / "logs"
        logdir.mkdir(exist_ok=True, mode=0o700)

        def drain():
            size = 0
            with (logdir / f"{launch_id}.log").open("wb") as log:
                for line in iter(backend.stdout.readline, b""):
                    if size < LOG_LIMIT:
                        log.write(line[:LOG_LIMIT - size])
                        log.flush()
                        size += len(line)
                    match = re.search(rb"opencode server listening on (http://127\.0\.0\.1:\d+)", line)
                    if match:
                        announcement.put(match[1].decode())

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        deadline = time.monotonic() + START_TIMEOUT
        endpoint = None
        while not stopping.is_set() and time.monotonic() < deadline and backend.poll() is None:
            try:
                endpoint = announcement.get(timeout=POLL_INTERVAL)
                break
            except queue.Empty:
                continue
        if not endpoint:
            raise DeckError("launch_failed", "OpenCode backend did not become ready; see local launch log")

        async def prepare():
            async with OpenCodeClient(Endpoint(endpoint), auth=(username, password)) as client:
                await client.health()
                conversation = (await client.get_session(attempt["directory"], attempt["session_id"]) if attempt["session_id"]
                                else await client.create_session(attempt["directory"], attempt["title"]))
            registration = Registration(attempt["workspace"], attempt["directory"], endpoint, conversation.id,
                                        pane.socket_path, pane.generation, pane.pane_id, attempt["title"] or "",
                                        attempt["reference_url"] or "", attempt["credential_ref"])
            return await register_session(registry, runtime, registration, native_runner=True,
                                          rebind_deck_id=attempt.get("rebind_deck_id"))

        session = asyncio.run(prepare())
        held_lock, resume_lock = resume_lock, None
        _release_resume_lock(held_lock)
        desired_session = session.registration.opencode_session_id
        while not stopping.is_set() and backend.poll() is None:
            argv = [env.get("SESSIONDECK_OPENCODE", "opencode"), "attach", endpoint, "--dir", attempt["directory"],
                    "--session", desired_session]
            tui = subprocess.Popen(argv, cwd=attempt["directory"], env=env)
            registry.update_launch(launch_id, state="ready", deck_id=session.deck_id, endpoint=endpoint,
                                    tui_pid=tui.pid, tui_session_id=desired_session)
            while tui.poll() is None and backend.poll() is None and not stopping.wait(POLL_INTERVAL):
                pass
            if terminal is not None:
                try:
                    termios.tcsetattr(0, termios.TCSANOW, terminal)
                except termios.error:
                    pass
            if stopping.is_set() or backend.poll() is not None or tui.returncode == 0:
                break
            registry.update_launch(launch_id, state="tui_lost", error="TUI exited unexpectedly; attach to recover")
            while not stopping.wait(POLL_INTERVAL) and backend.poll() is None:
                restart = registry.consume_tui_restart(launch_id)
                if restart is not None:
                    desired_session = restart["session_id"] or session.registration.opencode_session_id
                    break
            else:
                break
        registry.update_launch(launch_id, state="stopped", error="Session exited")
        return 0
    except (DeckError, OSError, KeyError) as exc:
        registry.update_launch(launch_id, state="failed", error=str(exc), waiting_for_resume_lock=False)
        return 1
    finally:
        try:
            terminate_process(tui)
            terminate_process(backend, group=True)
        finally:
            _release_resume_lock(resume_lock)
            if terminal is not None:
                try:
                    termios.tcsetattr(0, termios.TCSANOW, terminal)
                except termios.error:
                    pass
