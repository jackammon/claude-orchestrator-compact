# Architecture and reference review

Research checked against Claude Code documentation on 2026-10-10 and installed CLI 2.1.296.

## Official API findings

- [Hooks](https://code.claude.com/docs/en/hooks) expose `agent_id` and `agent_type`, `SessionStart`, main `Stop`, `PostCompact`, and `background_tasks`. Plugin hooks also run in subagents, so each hook must explicitly filter them. `Stop` means the main agent finished responding; a tool call returning does not imply idle.
- [Status line](https://code.claude.com/docs/en/statusline) reports accurate latest-response input/cache counts and context usage, but configuring it would replace the user's current status line. We use latest transcript usage instead and label it as potentially stale.
- [Commands](https://code.claude.com/docs/en/commands) document `/compact [instructions]` and its optional preservation instructions. No documented hook or CLI API was found that directly invokes `/compact` in an existing interactive session.
- [Plugins](https://code.claude.com/docs/en/plugins) package hooks in `.claude-plugin/plugin.json` and `hooks/hooks.json`.

## Reference: sure-scale/claude-code-auto-compact

[Source](https://github.com/sure-scale/claude-code-auto-compact), [delivery script](https://raw.githubusercontent.com/sure-scale/claude-code-auto-compact/main/bin/auto-compact.sh), [sensor](https://raw.githubusercontent.com/sure-scale/claude-code-auto-compact/main/bin/context-sensor.py), and [hooks](https://raw.githubusercontent.com/sure-scale/claude-code-auto-compact/main/hooks/hooks.json) were reviewed. The repository had six commits at inspection and uses the MIT license. Its packaging and input/cache token accounting are useful. Its delivery script self-forks from a tool call and sends `/compact` immediately, followed by timed sleeps and a prompt glyph search. It does not verify the main agent identity, an empty user input buffer, outstanding child work, or an exact session/pane binding. Its sensor includes subagent-specific prompting and default thresholds for a smaller context window. This is unsuitable for the orchestrator-only invariant. No code was copied; this implementation is independent.

## State machine

`SessionStart` binds a Claude `session_id` to one tmux pane ID, pane PID, TTY, and current command. Only `claude` panes are accepted. A random token is injected into the main session's context for requests. `PostToolUse` and `UserPromptSubmit` read the latest main transcript usage and report threshold transitions. An explicit request requires a readable, nonempty handoff under 32 KiB, an agent reconciliation assertion, and an assertion that no owner answer or approval is pending. `SubagentStart` and `SubagentStop` track children. `Stop` requires an empty tracked child set, a present empty task registry, and unchanged pane identity. The detached worker waits for a stable empty prompt, rechecks the request, and sends `/compact` to the bound pane. `PostCompact` confirms the manual compaction and, when enabled, queues continuation in that same pane. A new owner prompt cancels a queued request or pending continuation.

`queued -> stop_seen -> sent -> confirmed -> resume_sent` is the normal path. Requests may also be `cancelled`, `expired`, or `failed`. No state transition is allowed from a subagent hook.

## Reliability boundary

Claude Code does not expose an official interactive-idle/input-buffer API to command hooks. The tmux prompt check is heuristic and cannot prove the user will not type between check and injection. Therefore this is an experimental best-effort coordinator rather than a fully reliable unattended solution. A disposable Haiku 5.5 test validated the sequence under controlled conditions, but the plugin does not claim a race-free unattended guarantee until a safe official API or stronger terminal ownership protocol exists. It never writes to a child agent pane, but a collision with user input in the main pane remains possible.

## Attribution

Research reference: `sure-scale/claude-code-auto-compact`, MIT licensed. This project does not vendor its code. Documentation is attributed through links above. Claude Code is an Anthropic product; this plugin is independent.
