# Verification

## Automated checks

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/integration --live -q
```

The normal suite skips `live` tests. tmux integration tests create unique
sockets and clean up only those runtimes. Live tests isolate OpenCode's XDG
configuration/data and temporary project directories; they never use the user's
existing conversations. Test providers are local deterministic HTTP fixtures;
no paid model/provider is contacted.

The live suite exercises:

- OpenCode 1.19.2 health, authentication, conversation creation/read/deletion,
  omitted idle status, pending lists and real SSE event delivery.
- Actual TUI selection between two conversations with different visible text.
- Native backend/TUI launch, dashboard recreation, TUI crash and interactive
  recovery without a new backend, window destruction and abrupt runner death.
- A real OpenCode bash permission request generated using a local model fixture;
  allowing once or denying through the dashboard's keyboard actions and verifying
  completed/error tool states respectively.

The PTY navigation test attaches a real tmux client, uses `Ctrl-b d` to return
from an agent to the list, uses it again to detach, and checks the agent pane
survives both operations.

## Reporting boundaries

Initial implementation verification on the target platform:

| Check | Result |
| --- | --- |
| Normal suite | 41 passed; 9 opt-in live checks skipped |
| Integration suite with `--live` | 10 passed |
| Source distribution and wheel build | Passed |

Regression checks also cover healthy-project publication during another
endpoint's outage, responsive navigation during a pending attach/decision,
secondary-conversation recovery on a shared native pane, and TERM-resistant
same-group children after their backend leader exits.

## ID-only resume follow-up

`start --session SESSION_ID` now obtains the original directory through an
exact metadata API lookup. The project-scoped CLI listing is deliberately not
used for resolution: it depends on the caller's Git project and defaults to
100 records.

- Normal suite after this change: **60 passed, 12 opt-in live checks skipped**.
- Focused real integration run: **4 passed**, covering the listing-scope probe,
  actual CLI resume from another Git project context, resume beyond 101 newer
  conversations, and existing explicit-directory historical resume.
- Synthetic process tests cover readiness followed by a large log burst and
  cleanup of a TERM-resistant owned child after its leader exits.

The metadata helper sends no prompt, reads no transcript, and stops its own
short-lived server. These checks do not access user conversations.

## Completed-session cleanup and endpoint reuse

- Normal suite: **75 passed, 18 opt-in live checks skipped**.
- Full isolated integration suite: **19 passed**; launching real backends and
  TUIs can take several minutes.
- Regressions cover reused endpoints after pane loss/server replacement,
  protection of live and concurrent registrations, and native resume without
  removing historical records.
- Completed entries are hidden by default. Cleanup tests verify workspace scope,
  fresh completion checks, retained OpenCode history, and no effect on current
  agents or temporarily unreachable entries.
- Real dashboard permission tests resume over old records, allow/deny a tool
  request, and archive completed history while the current backend stays alive.
- A gated real-backend test verifies one-conversation startup serialization even
  after both callers return `starting`; the second runner never spawns a backend.
- SQLite-contention tests verify cleanup/status writes do not block the UI loop;
  event subscriptions refresh credentials when a local endpoint is reused.

Passing protocol/PTY tests is not a claim that a real phone or an unreliable
SSH connection was exercised. Physical-phone SSH reconnect and extended manual
use remain user acceptance checks. Linux/Python 3.12/tmux 3.4/OpenCode 1.19.2
are the initial verification target; other OpenCode releases are unsupported.
