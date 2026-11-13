# OpenCode SessionDeck

SSH-friendly terminal dashboard for monitoring and switching between OpenCode
sessions running in tmux.

- One place for your running agents, organized by workspace and directory.
- See working, idle, waiting, pending approval, stopped, and unreachable states.
- Approve once or deny a tool request directly from the dashboard.
- Keep the real OpenCode TUI for prompts, questions, and editing.
- Disconnect and reconnect over SSH without stopping the agents.
- Completed sessions disappear from the active view; their records can be
  inspected and cleared without deleting OpenCode history.

## Requirements

- Linux, Python 3.12+, tmux (tested with 3.4), a UTF-8 terminal.
- **OpenCode 1.19.2** on `PATH`. Other versions are not supported yet.

## Install

```sh
pipx install --python python3.12 .
```

For development:

```sh
uv venv --python python3.12
uv pip install -e '.[dev]'
```

## Start

```sh
sessiondeck start --workspace work --directory ~/projects/app --title "Review changes"
sessiondeck start --workspace personal --directory ~/projects/home
sessiondeck
```

`start` creates an OpenCode backend and TUI in a persistent tmux window. It does
not send a prompt; select the session in the dashboard and enter your message
in OpenCode. A project appears after its first session is registered.

**One key sequence: `Ctrl-b`, then `d`.** From an agent it returns to the list;
from the list it detaches your terminal. The agents keep working in both cases.
Run `sessiondeck` again from another SSH terminal, including your phone.

The dedicated socket is `opencode-sessiondeck`; your regular tmux server is
not modified. All clients share the selected window in v1.

To resume an existing conversation, its ID is enough; SessionDeck looks up
the original project directory:

```sh
sessiondeck start --workspace work --session SESSION_ID
```

Let the old agent finish and exit its original instance before resuming. This
restores the saved conversation, not an in-flight model request or tool process.

## Navigation

| Key | Action |
| --- | --- |
| `1` / `2` / `3` | Work / Personal / All project groups |
| Arrows, `Enter` | Choose a directory, then open its OpenCode session |
| `Escape` / `Backspace` | Back to directories |
| `a` / `d` | Allow the displayed permission once / deny it |
| `n` | Show the next pending permission |
| `r` | Refresh |
| `h` | Show/hide completed sessions |
| `c` | Archive completed records in the current workspace/directory view |
| `Ctrl-b d` | Agent → dashboard; dashboard → detach |

`d` alone is a permission decision; `Ctrl-b d` is handled by tmux. Questions
are visible in the dashboard but answered inside OpenCode. At narrow widths,
the table and details stack vertically and can be scrolled.

Cleanup rechecks that a session is completed; working and temporarily
unreachable agents are retained. To reload an already-running dashboard after
a local update, press `Ctrl+Q` in the dashboard and run `sessiondeck` again.
Only the panel exits; agent windows keep running.

## Integrate your launcher

An existing launcher can use the [local registration API](docs/registration-api.md).
SessionDeck does not depend on GitHub, GitLab, or an MR-specific launcher.

Plain terminals and arbitrary tmux servers are not auto-discovered: launch in
the managed runtime and explicitly register the OpenCode endpoint, conversation
and pane. Additional conversations created with OpenCode's `/new` also need
registration; a registered binding is not silently changed to another conversation.

See [usage and recovery](docs/usage.md) and [verification](docs/verification.md).

See [the architecture and design](docs/design.md).
