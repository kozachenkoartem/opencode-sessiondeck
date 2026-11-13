import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from sessiondeck import history, runner
from sessiondeck.cli import parser
from sessiondeck.models import Conversation, DeckError
from sessiondeck.registry import Registry
from sessiondeck.state import State


def metadata(monkeypatch, info=None, error=None):
    async def read(session_id):
        assert session_id == "ses_test"
        if error:
            raise error
        return info
    monkeypatch.setattr(history, "_read_metadata", read)


def test_lookup_preserves_original_directory(monkeypatch, tmp_path):
    directory = str(tmp_path / "space проект")
    metadata(monkeypatch, Conversation("ses_test", directory, "Title", 1))
    assert history.lookup_session_directory("ses_test") == directory


@pytest.mark.parametrize("directory", ["relative", "", 7, None, "/bad\x00directory"])
def test_malformed_metadata_is_not_a_directory(monkeypatch, directory):
    metadata(monkeypatch, Conversation("ses_test", directory, "Title", 1))
    with pytest.raises(DeckError) as error:
        history.lookup_session_directory("ses_test")
    assert error.value.code == "invalid_session_metadata"


def test_wrong_conversation_metadata_is_rejected(monkeypatch):
    metadata(monkeypatch, Conversation("ses_other", "/other", "Title", 1))
    with pytest.raises(DeckError) as error:
        history.lookup_session_directory("ses_test")
    assert error.value.code == "invalid_session_metadata"


def test_missing_session_does_not_fall_back_to_cwd(monkeypatch):
    metadata(monkeypatch, error=DeckError("not_found", "PRIVATE RESPONSE"))
    with pytest.raises(DeckError) as error:
        history.lookup_session_directory("ses_test")
    assert error.value.code == "session_not_found" and error.value.exit_code == 2
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("failure", [FileNotFoundError(), TimeoutError(), DeckError("unreachable", "PRIVATE RESPONSE")])
def test_lookup_failure_does_not_expose_details(monkeypatch, failure):
    metadata(monkeypatch, error=failure)
    with pytest.raises(DeckError) as error:
        history.lookup_session_directory("ses_test")
    assert error.value.code == "session_lookup_failed"
    assert "PRIVATE" not in str(error.value)


def test_invalid_id_is_rejected_before_lookup(monkeypatch):
    monkeypatch.setattr(history, "_read_metadata", lambda _: pytest.fail("Invalid IDs must not trigger lookup"))
    with pytest.raises(DeckError) as error:
        history.lookup_session_directory("ses_foo;bad")
    assert error.value.code == "invalid_session"


def test_start_parser_accepts_id_without_directory():
    args = parser().parse_args(["start", "--session", "ses_test"])
    assert args.directory is None and args.session == "ses_test"


@pytest.mark.parametrize("explicit", [False, True])
def test_start_resolves_only_when_directory_is_omitted(monkeypatch, tmp_path, explicit):
    registry = Registry(State(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    lookups = []

    def lookup(session_id):
        lookups.append(session_id)
        return str(project)

    class Captured(RuntimeError):
        pass

    class Runtime:
        def ensure(self):
            raise Captured

    monkeypatch.setattr(runner, "lookup_session_directory", lookup)
    with pytest.raises(Captured):
        runner.start_session(registry, Runtime(), "work", str(project) if explicit else None, session_id="ses_test")
    assert lookups == ([] if explicit else ["ses_test"])


def test_missing_resolved_directory_fails_before_runtime(monkeypatch, tmp_path):
    registry = Registry(State(tmp_path / "state"))
    monkeypatch.setattr(runner, "lookup_session_directory", lambda _: str(tmp_path / "deleted"))
    with pytest.raises(DeckError) as error:
        runner.start_session(registry, None, "work", session_id="ses_test")
    assert error.value.code == "invalid_directory"
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0


def synthetic_backend(monkeypatch, tmp_path, source):
    executable = tmp_path / "metadata-backend"
    executable.write_text(f"#!{sys.executable}\n" + source)
    executable.chmod(0o755)
    monkeypatch.setenv("SESSIONDECK_OPENCODE", str(executable))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def health(self):
            return {"healthy": True, "version": "1.19.2"}

        async def get_session(self, directory, session_id):
            return Conversation(session_id, str(tmp_path), "Fixture", 1)

    monkeypatch.setattr(history, "OpenCodeClient", Client)


async def test_readiness_is_found_before_trimming_a_log_burst(monkeypatch, tmp_path):
    synthetic_backend(monkeypatch, tmp_path, '''import sys,time
sys.stdout.write("opencode server listening on http://127.0.0.1:12345\\n" + "x" * 2048)
sys.stdout.flush()
time.sleep(300)
''')
    monkeypatch.setattr(history, "METADATA_TIMEOUT", 2)
    assert (await history._read_metadata("ses_test")).directory == str(tmp_path)


async def test_metadata_cleanup_kills_child_after_leader_exits(monkeypatch, tmp_path):
    pid_file = tmp_path / "owned-processes.json"
    synthetic_backend(monkeypatch, tmp_path, f'''import json,os,signal,time
from pathlib import Path
pid_file = Path({str(pid_file)!r})
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0,1,2):
        os.dup2(null, fd)
    pid_file.write_text(json.dumps({{"pid": os.getpid(), "group": os.getpgrp()}}))
    time.sleep(300)
else:
    while not pid_file.exists():
        time.sleep(.01)
    print("opencode server listening on http://127.0.0.1:12345", flush=True)
    time.sleep(300)
''')
    try:
        await history._read_metadata("ses_test")
        child = json.loads(pid_file.read_text())

        def running():
            try:
                return Path(f"/proc/{child['pid']}/stat").read_text().split(") ")[1][0] != "Z"
            except FileNotFoundError:
                return False

        async with asyncio.timeout(1):
            while running():
                await asyncio.sleep(.01)
    finally:
        if pid_file.exists():
            child = json.loads(pid_file.read_text())
            try:
                os.killpg(child["group"], signal.SIGKILL)
            except ProcessLookupError:
                pass
