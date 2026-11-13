import subprocess
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from sessiondeck.models import DeckError
from sessiondeck.registry import Registry
from sessiondeck.runner import start_session, terminate_process
from sessiondeck.state import State


def test_invalid_launch_directory(tmp_path):
    registry = Registry(State(tmp_path / "state"))
    with pytest.raises(DeckError):
        start_session(registry, None, "work", str(tmp_path / "missing"))


def test_terminate_owned_process_group():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        terminate_process(process, group=True)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cleanup_kills_resistant_child_after_leader_exits():
    code = """import os,signal,time
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(os.getpid(), flush=True)
time.sleep(300)
"""
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    def child_running():
        try:
            return Path(f"/proc/{child_pid}/stat").read_text().split(") ")[1][0] != "Z"
        except FileNotFoundError:
            return False
    try:
        terminate_process(parent, group=True)
        deadline = time.monotonic() + 1
        while child_running() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not child_running()
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait()
        parent.stdout.close()
