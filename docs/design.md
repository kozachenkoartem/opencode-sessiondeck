# OpenCode SessionDeck: Design

## Intent

`opencode-sessiondeck` is an SSH-friendly terminal dashboard for a person
running several independent OpenCode conversations at once. It removes the
need to keep and remember several terminal windows while preserving the normal
OpenCode TUI as the place where work and messages happen.

The dashboard is a dispatcher, not a second chat client. It gives one place to
find a running agent, see whether it needs attention, answer a permission
request, and attach to it.

## Goals

- Keep managed OpenCode sessions alive inside one named tmux server after SSH
  disconnects.
- Track all managed sessions, including sessions from different project paths,
  without combining unrelated project session lists.
- Show a session title, workspace, project path, last activity, and one clear
  status: working, waiting, needs approval, idle, or stopped.
- Surface the current permission request and allow or deny it in the dashboard.
- Attach the terminal to an agent's tmux window and return to the dashboard
  without stopping the agent.
- Separate `work` and `personal` sessions by default, with an `all` view.
- Launch new conversations or resume saved conversations from the CLI, including
  directory inference by conversation ID.
- Hide completed sessions from the active view and provide scoped cleanup while
  preserving conversation history.

## Non-goals for the first release

- Sending prompts or editing files from the dashboard.
- Replacing the OpenCode TUI.
- Exposing the dashboard directly on the network. Remote access is SSH to the
  machine hosting tmux.
- Automatically managing arbitrary pre-existing tmux/OpenCode panes that were
  not started or explicitly registered with SessionDeck.
- Arbitrary workspace taxonomy, team/multi-user support, or a web UI.

## User experience

The user runs `sessiondeck` to start or attach to the managed tmux server.
Its first window is the SessionDeck dashboard.

The dashboard defaults to `work` and shows registered project directories with
their aggregate attention state. Selecting a directory opens that directory's
own session list; sessions from other directories do not appear there. A
workspace overview may show aggregate counts, but it does not flatten projects
into one session list.

Within a project, the list shows a compact row like:

```
!  Review a merge request           ~/projects/app  needs approval  1m
*  Investigate benchmark failure    ~/projects/app  working         now
o  Update documentation             ~/projects/app  idle            14m
```

`Enter` switches the client to the selected agent's tmux window. One context-aware
tmux binding, `Ctrl-b d`, returns from an agent to the dashboard, and detaches the
client when already in the dashboard. Neither operation closes or interrupts
OpenCode, so reconnecting from a phone via SSH resumes the running environment.

For a `needs approval` row, the detail pane displays the action and resources
requested by OpenCode. The user can approve or deny that specific request from
the dashboard. The action never saves a broad permission rule by default.

## Managed session model

SessionDeck maintains a local registry. Every registered session contains:

- a stable SessionDeck ID;
- OpenCode session ID and local server endpoint;
- tmux session, window, and pane identifiers;
- title, workspace (`work` or `personal`), and project directory;
- creation and latest-activity times.

The supported launcher creates a tmux window and starts OpenCode there. It
obtains and records the OpenCode session identity and endpoint. Existing launch
scripts, including an MR-based launcher, remain external clients: they register
their resulting tmux pane and OpenCode identity through SessionDeck's stable
local interface. Manual registration is the fallback for already-running
sessions.

A registry record becomes `stopped` when its tmux pane no longer exists. It is
kept in history until archived rather than silently deleted. The default view
hides completed entries; `h` includes them and `c` archives completed records in
the current workspace or directory after rechecking their status.

Confirmed missing panes and superseded generations on the same tmux socket no
longer reserve recycled endpoint URLs. Existing records remain intact; live
bindings and newly inserted concurrent registrations retain ownership protection.

## Status source and rules

SessionDeck queries the OpenCode local server API and subscribes to its event
stream. OpenCode exposes session listing and activity, pending per-session
permissions, and a route for replying to an individual permission request.

The dashboard derives one displayed state in this order:

1. `stopped`: a native launch explicitly completed, the pane is confirmed
   dead/missing, its server generation was replaced, or the conversation is
   confirmed deleted on a healthy server.
2. `unknown/unreachable`: current status cannot be verified, including an API
   connection failure or an unsupported version. Retain and label cached data.
3. `needs approval`: OpenCode has a pending permission request.
4. `waiting`: OpenCode reports a pending question or a retry/backoff. Show the
   question or retry reason; answer questions in the attached OpenCode TUI.
5. `working`: OpenCode reports the session as busy, including running tools.
6. `idle`: a successful status snapshot confirms no active execution.

Silence alone is not evidence that an agent is waiting. Read-only health checks
must not reset the displayed time of last agent activity.

The title comes from OpenCode when available; the launcher-provided title is
used until OpenCode has one. The dashboard must visibly distinguish an API
connection failure from an agent being idle.

## Workspace and project selection

First release configuration has two fixed workspace sections, `work` and
`personal`. Project directories are not preconfigured: a directory appears in
the workspace navigation after its first session is registered. A registered
session is assigned to a workspace by its launcher or manual registration.

The dashboard filters to one workspace by default. Navigation follows
`workspace -> project directory -> session`; a back action returns to the
directory list. Creating a session from the dashboard first selects a
workspace, then a project directory; the project-directory picker is deferred
until after the tracking workflow is proven. An existing MR launcher is an
external alternative launch path that registers its sessions through
SessionDeck.

## External registration interface

SessionDeck exposes a versioned local CLI/API for launchers. Registration
accepts the workspace, project directory, tmux pane identity, OpenCode session
identity and endpoint, plus optional title and external reference URL (such as
an MR URL). It returns the stable SessionDeck ID.

SessionDeck owns validation, persistence, status tracking, archival, and the
dashboard. A launcher owns any provider-specific operation, such as resolving
an MR URL or preparing a checkout. SessionDeck never requires an MR launcher
to be installed and contains no corporate GitLab-specific behavior.

## Security and reliability

- OpenCode server endpoints are bound to loopback. SessionDeck reaches them
  locally; SSH is the only remote entry point in v1.
- Permission replies retain OpenCode's normal per-request semantics and are
  logged in SessionDeck's local registry.
- tmux remains the process supervisor. SessionDeck can restart without
  terminating agents and reconstructs its view from the registry plus tmux and
  OpenCode API checks.
- A failed dashboard must not make an agent inaccessible: the user can always
  use normal `tmux attach` and `tmux select-window` commands.

## Version and implementation boundary

The first release supports OpenCode `1.19.2`. Compatibility with older or future
OpenCode versions is outside the initial support boundary. The launcher uses the normal
`opencode` command and the selected directory; model, provider, profile, and
other OpenCode settings remain OpenCode configuration concerns.

The implementation uses Python 3.12+, Textual, HTTPX and a local SQLite registry.
Native runners own their backend and TUI independently of the dashboard. A
runner-held interprocess startup lock prevents concurrent resumes of the same
conversation; parent CLI timeouts do not release that protection. Saved session
directories are resolved through an exact metadata API lookup using a short-lived
authenticated local backend, avoiding project-scoped session-list limitations.

## Acceptance scenarios

1. Start two work sessions in different project directories and one personal
   session. The default view shows only work; `all` shows all three grouped by
   project.
2. Disconnect SSH while an agent works. Reconnect from another terminal or a
   phone and attach to the same tmux server; the dashboard and agent are still
   live.
3. An agent asks for a tool permission. Its row moves to `needs approval`, the
   request is visible, and an approval from the dashboard unblocks that agent.
4. Attach to an agent, then use the tmux dashboard binding. The agent remains
   alive and the dashboard is shown again.
5. Kill an agent window. Its entry becomes `stopped` and can be archived.

## Scope boundaries

- Dashboard creation of new sessions is a follow-up. V1 provides CLI launch and
  registration, plus switching between registered sessions; no directory picker
  is needed for the first release.
- The registration API is a versioned local CLI contract, not a new HTTP service.
  The external MR launcher is not part of this repository.
- OpenCode history and running instances are distinct. CLI launch may resume a
  known historical session ID in its original directory; v1 does not import all
  conversation history or discover arbitrary unregistered processes.
- Work and personal are organizational filters, not filesystem security boundaries.
