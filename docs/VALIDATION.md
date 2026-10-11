# Validation record

Tested on 2026-10-10 with Claude Code CLI 2.1.296, tmux 3.8, and Haiku 5.5 at low effort in `/private/tmp/oc-live-project`. The test config used 1, 2, and 3 token thresholds; the normal 200K/300K/400K policy was not changed. No active Headway session was used.

| Check | Result | Evidence |
| --- | --- | --- |
| Plugin discovery | Pass | `claude --plugin-dir ... plugin details` listed seven hooks. |
| Main session binding | Pass | Disposable tmux pane was bound by session ID, pane ID, PID, TTY, and `claude` command. |
| Context sensor | Pass | Latest transcript usage produced a threshold event and local `usage.json`. |
| Main request after Stop | Pass | A queued request became `stop_seen`, then `sent` after the main turn ended. |
| `/compact` delivery | Pass | Claude TUI displayed the `/compact` command and compact completion. |
| PostCompact and continuation | Pass | `PostCompact` confirmed; continuation read the handoff and replied `RESUMED`. |
| Model-directed request | Pass | The main Haiku session invoked the coordinator itself at a checkpoint; a second `/compact` and continuation completed. |
| Native child lifecycle | Pass | One native Explore subagent started and stopped; active tracking returned to empty and no plugin compaction targeted the child. |
| Multiple sessions | Pass | Two simultaneous Claude panes had distinct bindings; using the second session's token from the first pane failed. A request in the second pane was cancelled. |
| Pending user input | Pass in controlled test | Unsent text in the second pane caused delivery to fail without injecting `/compact`. This does not prove absence of a simultaneous keystroke race. |
| Automated tests | Pass | 17 local tests, including thresholds, gates, duplicate prevention, child tracking, resume, and stale telemetry. |

The first run reported 41,122 input tokens before compaction and a subsequent sample of 35,539 after compaction. These are latest-response input-context measurements, not a billed cost comparison. This small test does not establish cost savings or performance at 200K tokens.

The following remain unproven: race-free behavior when a person types at the exact instant of injection, long-running Headway continuity, and cost savings per completed work unit. The test sessions used a cheaper model; the user's Opus orchestrator was not used.
