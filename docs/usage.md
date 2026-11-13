# Usage and recovery

## Launch or resume

```sh
sessiondeck start --workspace work --directory ~/projects/app
sessiondeck start --workspace work --session SESSION_ID
sessiondeck
```

When `--session` is supplied without `--directory`, SessionDeck reads the
original directory from OpenCode's session metadata. `--directory` remains
required for a new conversation and can also be supplied explicitly on resume.
The historical conversation ID must belong to the selected directory.
Provider, model, plugins and other behavior use normal OpenCode configuration.
Dashboard launch/directory browsing is deferred; choose a directory in your
shell or use an external launcher with API v1.

`start` returns a JSON launch summary with `id`, `state`, and, when ready,
`deck_id`. If startup takes more than 30 seconds, it reports `starting` rather
than killing a potentially working agent. The project appears once registration
has completed; failed launch logs are under the local state directory.

## Move an existing conversation from another terminal

The `--session` option resumes an existing OpenCode conversation inside the
managed tmux runtime. It restores the saved conversation; it does not migrate
an in-progress model request or tool process from the old terminal.

1. Let the old agent finish its current operation, or interrupt it deliberately.
2. Find the conversation ID locally with `opencode session list`.
3. Exit the old OpenCode instance, then launch the same conversation:

```sh
sessiondeck start --workspace work --session SESSION_ID
sessiondeck
```

Replace `SESSION_ID` with the local conversation ID. The original directory is
resolved with a direct metadata lookup by ID through OpenCode's local API,
without reading transcripts or accessing its database directly. SessionDeck uses
a short-lived, authenticated loopback server for this lookup, then stops it;
the lookup does not execute a prompt. It works independently of the caller's
project and the session-list limit. Unknown IDs, invalid metadata, and deleted
directories do not fall back to the caller's current directory or provision a
new agent. You can also supply `--directory` explicitly to skip the lookup.

Use the same OpenCode data environment as the original instance. Do not submit new
work for that conversation from both instances concurrently. After opening it
in SessionDeck, send further instructions normally in its OpenCode TUI.

## One terminal, many agents

Run `sessiondeck` outside other tmux servers. Select Work, Personal or All, then
a directory and a session. Every other agent continues running while you work
in the selected OpenCode TUI.

Press `Ctrl-b d` to leave the agent and return to the list. Press it in the
list to detach from tmux. Closing an SSH connection also leaves the agents
alive. Run `sessiondeck` on reconnect to return to the list.

This changes tmux's detach key only in the dedicated SessionDeck server. From
an agent, the first key sequence returns to the list rather than disconnecting
SSH. To detach in one shell command, use `tmux -L opencode-sessiondeck detach-client`.

Several clients attached to the same `main` tmux session share window selection.
Independent simultaneous phone/desktop views are not implemented in v1.

## States and decisions

The normal view shows active sessions. Confirmed completed sessions are hidden
automatically, including projects with no active sessions. Press `h` to include
completed records. Enter on a completed record shows its status rather than
attempting to attach to a nonexistent pane.

Press `c` to archive completed records in the current workspace; inside a
directory, cleanup is restricted to that directory. Status is rechecked before
archiving. Working or temporarily unreachable sessions are not cleared, and no
agent processes are terminated. Archived records remain available through
`api v1 list --include-archived`, and `api v1 unarchive` restores their tracking.

- **working:** OpenCode reports busy, including running tools.
- **checking:** a newly registered session is waiting for its first snapshot.
- **needs approval:** a tool permission is pending; inspect it, then allow once
  or deny. No persistent allow rule is created.
- **waiting:** a question needs an answer in OpenCode, or a retry/backoff has a reason.
- **idle:** a successful API snapshot confirms no active execution.
- **unreachable:** the API/auth/schema could not be verified; cached title and
  activity are retained and visibly labelled, not treated as idle.
- **stopped:** the registered pane is gone, its server generation was replaced,
  a native launch completed, or a healthy server confirms the conversation was
  deleted. Missing runtime metadata alone is treated as unreachable.

Activity is the conversation's update time, not the last dashboard poll.
Projects outside the current filter are still monitored for attention counts.
Each agent updates independently: a slow endpoint does not delay healthy
projects. Permission/question waits, provider retries and unreachable endpoints
contribute to project attention counts. Cached values are labelled while a
refresh is pending; event bursts are coalesced rather than polling per token.
After an ambiguous permission timeout the result remains unknown; request
disappearance is not proof that SessionDeck's decision succeeded.

## Recovery

If only a native TUI crashes, its backend is retained; selecting the session
recreates the TUI without starting another backend. A successful OpenCode TUI
exit is treated as intentional session shutdown; use `Ctrl-b d` to leave work
running instead of `/exit`.

A failed or killed dashboard can be recreated by running `sessiondeck`. It
does not own agent processes. You can always bypass it:

```sh
tmux -L opencode-sessiondeck attach-session -t main
tmux -L opencode-sessiondeck list-windows -t main
```

Native runners clean up their owned backend on termination/window destruction.
Linux parent-death signaling protects the bound backend on abrupt runner loss;
it is not a recursive cleanup guarantee for every possible tool descendant.
External launchers are responsible for their own lifecycle and recovery.

Stopped records are hidden by default and can be archived with `c` or API v1.
Reused local endpoints do not require manual unregister when the old pane is
confirmed gone or its tmux server generation was replaced. Explicit unregister
remains available for deliberate reassignment of an existing binding. Neither
operation deletes OpenCode history.

After updating the local source, reload an existing dashboard with `Ctrl+Q`
while in the dashboard, then run `sessiondeck`. Agents in other tmux windows
continue running; the replacement dashboard reconnects to them.

## Local state

State defaults to `$XDG_STATE_HOME/opencode-sessiondeck`, or
`~/.local/state/opencode-sessiondeck`. It contains the SQLite registry, private
credential files and bounded per-launch backend logs. `SESSIONDECK_STATE_DIR`
overrides it. `SESSIONDECK_TMUX_SOCKET` chooses another named socket for testing
or a separate installation; use matching state/socket settings in every caller.

Native agents inherit the managed tmux runtime's environment. Credentials
persisted by OpenCode and project-specific configuration remain OpenCode's
responsibility. Workspace labels organize sessions; they are not filesystem
security boundaries.
