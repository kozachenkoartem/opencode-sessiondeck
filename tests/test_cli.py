import json
import os
import subprocess
import sys


def test_invalid_json_envelope_and_state_reopen(tmp_path):
    env = os.environ | {"SESSIONDECK_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run([sys.executable, "-m", "sessiondeck", "api", "v1", "register"],
                            input="not json", text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    result = subprocess.run([sys.executable, "-m", "sessiondeck", "api", "v1", "list"],
                            text=True, capture_output=True, env=env)
    assert result.returncode == 0
    assert json.loads(result.stdout)["data"] == []


def test_start_needs_directory_or_session_before_provisioning(tmp_path):
    state = tmp_path / "state"
    result = subprocess.run([sys.executable, "-m", "sessiondeck", "start"],
                            text=True, capture_output=True, env=os.environ | {"SESSIONDECK_STATE_DIR": str(state)})
    assert result.returncode == 2
    assert "--directory" in result.stderr and "--session" in result.stderr
    assert not state.exists()
