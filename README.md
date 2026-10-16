# claude-orchestrator-compact

A local Claude Code plugin for phase-aware context checkpoints in a **main orchestrator**. It observes the main conversation's reported input token use, asks the orchestrator to decide when a coherent phase has ended, and queues `/compact` for the main tmux pane after Claude Code's main `Stop` event. It does not change models, effort, permissions, context-window size, or Claude Code's built-in safety compaction.

**Status: experimental.** The coordinator and its safety gates have automated tests. The full request, `/compact`, `PostCompact`, and continuation path was validated in a disposable Haiku 5.5 session. The tmux screen check cannot eliminate a simultaneous user keystroke race. Keep a handoff and inspect the log before relying on this for important work.

## Architecture

```mermaid
flowchart LR
  A[Main Claude Code CLI in tmux] --> B[Plugin hooks]
  B --> C[Local token sensor and per-session state]
  C --> D[Main orchestrator chooses phase checkpoint]
  D --> E[Durable project handoff]
  E --> F[Queued request]
  F --> G[Main Stop hook checks background tasks and pane identity]
  G --> H[Detached worker checks empty idle prompt]
  H --> I[/compact in exact tmux pane]
  I --> J[PostCompact confirms and queues continuation]
```

The plugin hooks run in subagents too, because Claude Code applies plugin hooks there. Every hook checks `agent_id` and `agent_type` and returns without side effects for child agents. A request can only be delivered from the bound **main** session's `Stop` hook. `SubagentStart` and `SubagentStop` track active children; the main `Stop` also requires an empty background task registry. Native subagent auto-compaction remains Claude Code's responsibility.

## Requirements

- Claude Code CLI 2.1.296 or newer (the tested local version)
- Python 3.9 or newer
- tmux 3.8 or newer on macOS or Linux

## Install

1. Install tmux: `brew install tmux` (macOS).
2. Clone this repository. The wrapper loads this checkout using `claude --plugin-dir` without changing global Claude Code settings. For other launch methods, pass `--plugin-dir /absolute/path/to/claude-orchestrator-compact`, or install it from a private/local marketplace; see [Claude Code plugins](https://code.claude.com/docs/en/plugins) and [marketplace documentation](https://code.claude.com/docs/en/plugin-marketplaces).
3. Start an orchestrator from a repository with `bin/claude-orchestrator`. This creates a uniquely named tmux session and runs `claude`. The wrapper deliberately takes no Claude flags; for custom flags, start `tmux new -s your-name`, then run `claude ...` yourself.
4. Detach with `Ctrl-b d`; list sessions with `tmux ls`; reattach with `tmux attach -t SESSION_NAME`.

The plugin does not edit `~/.claude/settings.json`, shell startup files, or agent definitions. This preserves the usual owner feedback and PR review flow: the orchestrator receives your messages in its main pane and can route them to agents. It should request compaction only after those decisions are resolved. You may copy `bin/claude-orchestrator` into a directory on your `PATH` or add this repo's `bin` directory to `PATH` yourself.

## Checkpoint policy

Reminders fire at **350K, 500K and 600K input tokens** (configurable with `reminderThresholdsTokens`), based on the latest main conversation API response, including cache-read and cache-write tokens. The main orchestrator can request compaction at any earlier or later coherent checkpoint. Each threshold reminds once and may be deferred until a good stopping place; if context jumps past several at once, one reminder covers them. A threshold rearms after context falls back below it, for example after compaction. Native safety auto-compaction remains unchanged.

`SessionStart` gives the **main orchestrator** that policy, the handoff checklist, and a session-specific request command. The orchestrator signals by running the local `request` command through its Bash tool; it does not print `/compact` or attempt to run that slash command itself. The request records its decision, and the coordinator handles `/compact` after the main turn ends. Child agents receive no such instruction.

At a checkpoint:

1. Background tasks and subagents may keep running: Claude Code keeps them going through `/compact`, and their completion notices still arrive afterwards. List each one still running in the handoff (id, what it is doing, what to do with its result). The `Stop` hook also records the `background_tasks` it sees and the continuation prompt names them. Set `requireNoBackgroundWork` to `true` to restore the old behaviour of waiting until all child work has finished.
2. Save a concise durable handoff in the project's existing planning location. Record what is done, outstanding work, agent results, branch/PR/CI state, decisions, blockers, and next actions. Keep it under 32 KiB.
3. Use the session-specific command injected by `SessionStart` to queue the request, supplying the handoff path, a short reason, `--owner-input-clear` (plus `--agents-reconciled` when `requireNoBackgroundWork` is on). Assert the last flag only when no owner answer, review, or approval is pending. No token minimum or `--force` flag applies.
4. Allow the main agent to finish its turn. The `Stop` hook queues delivery; the worker checks that the exact bound tmux pane still exists and shows a stable empty prompt.
5. `PostCompact` confirms the manual compaction. With `resumeAfterCompaction` enabled, a second worker waits for the same pane to show a stable empty prompt, then asks Claude to read the handoff, check repository status, and continue. This path passed a disposable Haiku 5.5 test.

A new owner prompt cancels a queued request or pending continuation. The request expires after five minutes. A confirmed compaction is debounced for five minutes. Use `python3 bin/orchestrator_compact.py status --session UUID` to inspect state and `cancel --session UUID` to cancel a queued request.

## Configuration

Copy [`config.example.json`](config.example.json) to `~/.config/claude-orchestrator-compact/config.json`, or set `ORCH_COMPACT_CONFIG` to another path. `ORCH_COMPACT_STATE_DIR` controls the private local state and log directory (default `~/.local/state/claude-orchestrator-compact`). Set `enabled` to `false` to disable. The example enables continuation. Set `resumeAfterCompaction` to `false` if you want compaction to return to an idle prompt. The coordinator requires an explicit owner-clearance assertion before queueing.

Each session has `usage.json`, `binding.json`, `request.json`, `agents.json`, and `events.jsonl`. The event log contains timestamps, counters, crossing of the reminder threshold, and request state. It never logs the conversation or compact summary. `binding.json` contains a session token, so keep the state directory private.

## Cost measurement

Track completed tickets or other stable work units, input/output/cache token counts, and time across comparable periods with and without the plugin. Compare cost per completed unit and cache hit ratio, not only context reduction. Transcript usage is the **latest reported API response** and may lag hook execution; it is not a live exact total. The plugin does not estimate billed dollars.

## Disable and uninstall

Set `enabled` to `false` in the config and restart Claude Code, or uninstall/disable the plugin using Claude Code's `/plugin` menu. Remove this repo's wrapper from your `PATH` if you added it. Remove `~/.config/claude-orchestrator-compact` and `~/.local/state/claude-orchestrator-compact` only if you no longer need the logs. tmux can remain installed for unrelated use.

## Troubleshooting and limitations

- No reminder: confirm the plugin loaded, the session is the main agent, and the transcript has usage data. Hooks skip agents with `agent_id` or `agent_type`.
- Queued but not sent: check `background_tasks` (only with `requireNoBackgroundWork`), request expiry, pane identity, and the exact empty prompt in `events.jsonl`. The worker fails closed on ambiguity.
- Sent but not confirmed: check whether `/compact` ran and whether `PostCompact` fired. Do not assume a successful `tmux send-keys` means compaction succeeded.
- Owner feedback is pending: do not queue compaction. If you type a new prompt before delivery, the hook cancels the request. To cancel explicitly, run `python3 bin/orchestrator_compact.py cancel --session UUID`.
- You can always attach with `tmux attach -t SESSION_NAME`, type to Claude normally, and detach with `Ctrl-b d`. Keep the main orchestrator in its own pane; child agents do not receive terminal input from this plugin.
- A prompt drawn by another program could look like Claude's prompt; the binding requires `pane_current_command` to be `claude`, but tmux visual inspection is not an official Claude idle API. User input can race with delivery. Do not leave a queued request while typing in the target pane.
- The plugin does not parse or mutate Claude Code's history, control child panes, or alter subagent context settings.
- No hosted service or additional Python packages are needed.

## Development

Run `PYTHONPYCACHEPREFIX=/tmp/pycache-orch python3 -m unittest discover -s tests -v`. See [architecture and reference review](docs/ARCHITECTURE.md) and [validation record](docs/VALIDATION.md).
