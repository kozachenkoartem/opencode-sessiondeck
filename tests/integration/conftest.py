import asyncio
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sessiondeck.models import Endpoint
from sessiondeck.opencode import OpenCodeClient


@pytest.fixture
def local_provider():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tools = {tool["function"]["name"] for tool in data.get("tools", [])}
            tool_done = any(message["role"] == "tool" for message in data.get("messages", []))
            if "bash" in tools and not tool_done:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_deck_probe", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "pwd", "description": "Print test directory"})}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "Local test complete"}, "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for payload, reason in [(delta, None), ({}, finish)]:
                chunk = {"id": "chatcmpl-deck", "object": "chat.completion.chunk", "model": "fixture", "created": 1,
                         "choices": [{"index": 0, "delta": payload, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
async def live_server(tmp_path, local_provider):
    env = os.environ.copy()
    for key in ("CONFIG", "DATA", "CACHE", "STATE"):
        env[f"XDG_{key}_HOME"] = str(tmp_path / key.lower())
    env.pop("OPENCODE_CONFIG", None)
    env.pop("TMUX", None)
    provider = {"sessiondeck-fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                "options": {"baseURL": local_provider, "apiKey": "local-fixture"},
                "models": {"fixture": {"name": "Fixture", "limit": {"context": 8192, "output": 512}}}}}
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps({"plugin": [], "mcp": {}, "share": "disabled", "permission": {"bash": "ask"},
               "provider": provider, "model": "sessiondeck-fixture/fixture", "small_model": "sessiondeck-fixture/fixture"}),
               OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_PURE="1",
               OPENCODE_SERVER_PASSWORD="sessiondeck-test-password", OPENCODE_DISABLE_AUTOUPDATE="1")
    directory = tmp_path / "project space"
    directory.mkdir()
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0",
        cwd=directory, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(30):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError("Test server exited before readiness")
                match = re.search(rb"http://127\.0\.0\.1:\d+", line)
                if match:
                    endpoint = match[0].decode()
                    break
        async with OpenCodeClient(Endpoint(endpoint), auth=("opencode", env["OPENCODE_SERVER_PASSWORD"])) as client:
            yield client, proc, env, str(directory)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
