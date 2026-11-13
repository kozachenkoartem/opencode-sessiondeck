# Local registration API v1

The integration surface is a subprocess CLI, not an HTTP service or Python
import API. Consumers may use any language. Registration is explicit: SessionDeck
does not infer a conversation from process names, titles, or a directory alone.

## Runtime provisioning

```sh
sessiondeck api v1 runtime
```

The response's `data` contains `socket_path`, `generation`, `session_id`, and
`dashboard_pane_id`. An external launcher starts its agent/TUI pane on that
socket, for example using tmux's `-S SOCKET_PATH new-window -d -t main: -P
-F '#{pane_id}' -c DIRECTORY ...` with separate command arguments.

Keep the backend alive independently of the caller. A launcher owns its backend
and TUI lifecycle; registration does not supervise an external launcher. The
launcher attests that the supplied TUI pane really connects to the supplied
endpoint. Each endpoint must have only one TUI pane. Several explicitly
registered conversation IDs may share that pair.

## Register

```sh
sessiondeck api v1 register < registration.json
```

`registration.json` is one object. These are illustrative placeholders, not
values to reuse:

```json
{
  "workspace": "work",
  "directory": "/absolute/project",
  "endpoint": "http://127.0.0.1:4096",
  "opencode_session_id": "ses_EXAMPLE",
  "tmux_socket": "/tmp/tmux-1000/opencode-sessiondeck",
  "tmux_generation": "UUID_FROM_RUNTIME",
  "tmux_pane_id": "%3",
  "title": "Review changes",
  "reference_url": "https://github.com/example/project/pull/123",
  "auth_env": "OPENCODE_SERVER_PASSWORD",
  "username": "opencode"
}
```

Required: workspace (`work` or `personal`), existing directory, loopback HTTP
endpoint with explicit port, OpenCode ID, and tmux socket/generation/pane ID.
Title, URL, auth environment-variable name and username are optional. URLs are
metadata only: no provider-specific API is contacted.

`auth_env` names a variable in the registration process environment. Its value
is imported into a private local credential file so another SSH shell can
monitor the same agent without inheriting the old environment. Never put a
password in JSON, a URL, or a tmux command argument.

SessionDeck checks the running server version, conversation directory and live
pane before accepting registration. Worktrees in different directories remain
different project groups. Symlink aliases are canonicalized for grouping.

An identical registration returns the same `data.deck_id`. Different endpoints
claiming one pane, one endpoint claiming multiple panes, or workspace/directory
reassignment of an existing binding are conflicts. Old server generations cannot
claim reused pane IDs. If a previous pane is confirmed absent, or its server
generation has been replaced on the same socket, its historical record no longer
reserves a reused endpoint. The record and OpenCode history remain intact.

## Response format

Success:

```json
{"api_version": 1, "ok": true, "data": {"deck_id": "EXAMPLE", "registration": {}}}
```

Registration returns the full stored session record in `data`: `deck_id`,
`registration`, `api_directory`, `binding`, archive/retired flags and timestamps.
Credential references are opaque local filenames, never credential values.
Consumers should use named fields and tolerate additional response fields.

Failure:

```json
{"api_version": 1, "ok": false, "error": {"code": "binding_conflict", "message": "..."}}
```

One JSON object is written to stdout; diagnostics are separate from this
envelope. Exit statuses: `0` success, `2` invalid input, `3` unavailable or
incompatible dependency/state, `4` conflicting or stale binding.

## Listing and lifecycle metadata

```sh
sessiondeck api v1 list --workspace work --directory /absolute/project
sessiondeck api v1 list --include-archived
sessiondeck api v1 archive DECK_ID
sessiondeck api v1 unarchive DECK_ID
sessiondeck api v1 unregister DECK_ID
```

List returns registration metadata, not live agent status. Archive hides an
entry but retains ownership while its pane/runtime still exists. Unregister
retires tracking and releases ownership after the last conversation using that
endpoint/pane retires. Both preserve history and leave processes running. Use
unregister explicitly before deliberately reassigning an existing binding;
confirmed missing panes and superseded server generations are handled
automatically. SessionDeck never deletes an OpenCode conversation through this API.
