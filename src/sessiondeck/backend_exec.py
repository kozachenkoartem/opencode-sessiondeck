import ctypes
import os
import signal


def exec_backend(expected_parent: int):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected_parent:
        raise SystemExit(1)
    executable = os.environ.get("SESSIONDECK_OPENCODE", "opencode")
    os.execvpe(executable, [executable, "serve", "--hostname", "127.0.0.1", "--port", "0"], os.environ)
