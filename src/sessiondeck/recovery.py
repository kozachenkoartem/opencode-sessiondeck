from .models import DeckError
from .runner import start_session


class RecoveryCoordinator:
    def __init__(self, registry, runtime):
        self.registry = registry
        self.runtime = runtime

    def recover_pending(self):
        if not self.registry.consume_recovery_request():
            return ()
        restored = []
        for session in self.registry.list(include_archived=True):
            if not session.native_runner or not session.restore_on_start:
                continue
            try:
                self.runtime.inspect(session.binding.pane_id, session.binding.generation)
                continue
            except DeckError:
                pass
            try:
                result = start_session(self.registry, self.runtime, session.registration.workspace,
                                       session.registration.directory, session.registration.opencode_session_id,
                                       session.registration.title or None, session.registration.reference_url or None)
                if result["deck_id"] != session.deck_id:
                    self.registry.unregister(session.deck_id)
                restored.append(result["deck_id"])
            except DeckError:
                continue
        return tuple(restored)
