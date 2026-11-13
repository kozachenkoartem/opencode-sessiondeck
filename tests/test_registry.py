import concurrent.futures
from dataclasses import replace

import pytest

from sessiondeck.models import Conversation, DeckError, PaneBinding, Registration, VerifiedBinding
from sessiondeck.registry import Registry
from sessiondeck.state import State


@pytest.fixture
def registry(tmp_path):
    return Registry(State(tmp_path / "state"))


def binding(tmp_path, n=1, workspace="work"):
    directory = tmp_path / f"project {n}"
    directory.mkdir(exist_ok=True)
    pane = PaneBinding("/tmp/deck-test-socket", "generation", "$0", f"@{n}", f"%{n}")
    conversation = Conversation(f"ses_{n}", str(directory), f"Session {n}", 1000)
    reg = Registration(workspace, str(directory), f"http://127.0.0.1:{12000+n}", conversation.id,
                       pane.socket_path, pane.generation, pane.pane_id)
    return reg, VerifiedBinding(pane, conversation)


def test_duplicate_registration_is_idempotent(registry, tmp_path):
    reg, verified = binding(tmp_path)
    assert registry.register(reg, verified).deck_id == registry.register(reg, verified).deck_id


def test_parallel_registration_preserves_all_rows(registry, tmp_path):
    values = [binding(tmp_path, n) for n in range(12)]
    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(lambda args: registry.register(*args), values))
    assert len(registry.list()) == 12
    assert len(Registry(registry.state).list()) == 12


def test_workspace_or_directory_conflict(registry, tmp_path):
    reg, verified = binding(tmp_path)
    registry.register(reg, verified)
    with pytest.raises(DeckError) as exc:
        registry.register(replace(reg, workspace="personal"), verified)
    assert exc.value.exit_code == 4


def test_binding_cardinality_and_retirement(registry, tmp_path):
    reg, verified = binding(tmp_path)
    one = registry.register(reg, verified)
    with pytest.raises(DeckError):
        registry.register(replace(reg, endpoint="http://127.0.0.1:13000"), verified)
    pane = replace(verified.pane, pane_id="%9")
    with pytest.raises(DeckError):
        registry.register(replace(reg, tmux_pane_id="%9"), replace(verified, pane=pane))
    two = registry.register(replace(reg, opencode_session_id="ses_other"),
                            replace(verified, conversation=replace(verified.conversation, id="ses_other")))
    registry.set_archived(one.deck_id, True)
    registry.unregister(one.deck_id)
    with pytest.raises(DeckError):
        registry.register(replace(reg, endpoint="http://127.0.0.1:13000"), verified)
    registry.unregister(two.deck_id)
    assert registry.register(replace(reg, endpoint="http://127.0.0.1:13000"), verified)
    assert len(registry.list(include_archived=True, include_retired=True)) == 3


def test_worktrees_stay_separate_and_symlinks_group(registry, tmp_path):
    for n in (1, 2):
        registry.register(*binding(tmp_path, n))
    assert len({item.registration.directory for item in registry.list()}) == 2
    (tmp_path / "alias").symlink_to(tmp_path / "project 1")
    assert len(registry.list(directory=tmp_path / "alias")) == 1


def test_future_schema_is_not_rewritten(registry):
    with registry.connect() as db:
        db.execute("PRAGMA user_version=42")
    with pytest.raises(DeckError):
        Registry(registry.state)
    with registry.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 42


def test_credentials_are_private(registry):
    ref = registry.state.save_credential("opencode", "not-a-real-secret")
    assert registry.state.auth(ref) == ("opencode", "not-a-real-secret")
    assert (registry.state.root / "credentials" / ref).stat().st_mode & 0o777 == 0o600
    with pytest.raises(DeckError):
        registry.state.auth("../../credentials")


def test_coalescing_restart_request(registry):
    launch = registry.create_launch({"workspace": "work"})
    registry.update_launch(launch["id"], restart_tui=True)
    registry.update_launch(launch["id"], restart_tui=True)
    assert registry.consume_tui_restart(launch["id"])
    assert not registry.consume_tui_restart(launch["id"])


def test_obsolete_binding_releases_endpoint_without_removing_history(registry, tmp_path):
    reg, verified = binding(tmp_path)
    old = registry.register(reg, verified)
    replacement, current = binding(tmp_path, 2)
    replacement = replace(replacement, endpoint=reg.endpoint)
    new = registry.register(replacement, current, obsolete_bindings={old.deck_id: old.binding})
    assert new.deck_id != old.deck_id
    assert not registry.get(old.deck_id).retired
    assert len(registry.list()) == 2


def test_obsolete_whitelist_does_not_release_new_concurrent_binding(registry, tmp_path):
    reg, verified = binding(tmp_path)
    old = registry.register(reg, verified)
    obsolete = {old.deck_id: old.binding}
    second, current = binding(tmp_path, 2)
    registry.register(replace(second, endpoint=reg.endpoint), current, obsolete_bindings=obsolete)
    third, later = binding(tmp_path, 3)
    with pytest.raises(DeckError) as error:
        registry.register(replace(third, endpoint=reg.endpoint), later, obsolete_bindings=obsolete)
    assert error.value.code == "binding_conflict"


def test_obsolete_binding_requires_matching_fingerprint(registry, tmp_path):
    reg, verified = binding(tmp_path)
    old = registry.register(reg, verified)
    second, current = binding(tmp_path, 2)
    with pytest.raises(DeckError) as error:
        registry.register(replace(second, endpoint=reg.endpoint), current,
                          obsolete_bindings={old.deck_id: replace(old.binding, generation="wrong")})
    assert error.value.code == "binding_conflict"


def test_claim_is_revoked_by_explicit_unload(registry, tmp_path):
    session = registry.register(*binding(tmp_path), native_runner=True)
    claim = registry.claim_start(session.deck_id)
    group = registry.begin_unload(session.deck_id, (session.deck_id,))
    assert group.deck_ids == (session.deck_id,)
    assert not registry.validate_claim(claim)
    assert not registry.get(session.deck_id).restore_on_start


def test_unload_rejects_membership_that_changed_since_confirmation(registry, tmp_path):
    first = registry.register(*binding(tmp_path), native_runner=True)
    second = registry.register(replace(first.registration, opencode_session_id="ses_shared"),
                               VerifiedBinding(first.binding, replace(
                                   binding(tmp_path, 2)[1].conversation,
                                   id="ses_shared", directory=first.registration.directory)),
                               native_runner=True)
    with pytest.raises(DeckError, match="membership changed"):
        registry.begin_unload(first.deck_id, (first.deck_id,))
    assert registry.get(first.deck_id).restore_on_start
    assert registry.get(second.deck_id).restore_on_start


def test_external_registration_is_not_recoverable(registry, tmp_path):
    session = registry.register(*binding(tmp_path))
    assert not session.native_runner
    assert not session.restore_on_start


def test_rebind_preserves_logical_session_identity(registry, tmp_path):
    reg, verified = binding(tmp_path)
    original = registry.register(reg, verified, native_runner=True)
    pane = replace(verified.pane, pane_id="%9", generation="replacement")
    replacement = registry.register(replace(reg, endpoint="http://127.0.0.1:19000", tmux_pane_id=pane.pane_id,
                                            tmux_generation=pane.generation),
                                    replace(verified, pane=pane), native_runner=True, rebind_deck_id=original.deck_id)
    assert replacement.deck_id == original.deck_id
    assert replacement.binding == pane
