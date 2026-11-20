---
name: sessiondeck-new-session
description: Use when the user asks to create, start, or launch OpenCode sessions through SessionDeck, including batches of planning sessions for tracked subtasks.
---

# Create SessionDeck Sessions

Create OpenCode sessions only through SessionDeck.

1. Ask for a project directory unless the user supplied one. For a batch request, confirm the directory once and use it for every session in that batch.
2. Require an absolute existing directory. Do not infer it from the current directory or reuse it for a later, separate request.
3. Create work sessions with:

```bash
sessiondeck start --workspace work --directory "<absolute-directory>"
```

4. Report the returned SessionDeck ID and state for every created session. Do not attach to, stop, unload, or otherwise modify other sessions.

## Planning Task Batches

When the user asks to create planning sessions for tracked subtasks:

1. Read the parent task and its subtasks with the appropriate task-tracker integration.
2. Apply only the status filter stated by the user. Do not assume a default filter.
3. Before creating sessions, report the selected task IDs, target directory, and prompt template, then obtain one confirmation.
4. Create one session per selected task:

```bash
sessiondeck start --workspace work --directory "<absolute-directory>" --title "<task ID>: <summary>" --agent plan --prompt "Task: <task URL>\n\n<shared instruction>"
```

5. Keep the task URL in the initial prompt and use the exact shared instruction supplied by the user.
