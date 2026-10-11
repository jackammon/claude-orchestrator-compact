#!/usr/bin/env python3
"""Fail-closed, main-session-only Claude Code compaction coordinator."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULTS = {"enabled": True, "reminderThresholdTokens": 600000,
            "requestTtlSeconds": 300, "debounceSeconds": 300,
            "requireNoActiveSubagents": True,
            "requireOwnerInputClear": True,
            "preserveHandoff": True, "resumeAfterCompaction": True,
            "telemetryEnabled": True}
UUID = re.compile(r"^[0-9a-fA-F-]{16,64}$")
PANE = re.compile(r"^%[0-9]+$")


def root() -> Path:
    return Path(os.environ.get("ORCH_COMPACT_STATE_DIR", str(Path.home() / ".local/state/claude-orchestrator-compact")))


def config() -> dict:
    cfg = DEFAULTS.copy()
    path = Path(os.environ.get("ORCH_COMPACT_CONFIG", str(Path.home() / ".config/claude-orchestrator-compact/config.json")))
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - set(cfg):
            raise ValueError("invalid configuration keys")
        cfg.update(data)
    if type(cfg["reminderThresholdTokens"]) is not int or cfg["reminderThresholdTokens"] < 0:
        raise ValueError("invalid reminderThresholdTokens")
    for k in ("requestTtlSeconds", "debounceSeconds"):
        if type(cfg[k]) is not int or cfg[k] < 0:
            raise ValueError(f"invalid {k}")
    for k in ("enabled", "requireNoActiveSubagents",
              "requireOwnerInputClear", "preserveHandoff", "resumeAfterCompaction",
              "telemetryEnabled"):
        if type(cfg[k]) is not bool:
            raise ValueError(f"invalid {k}")
    if not cfg["preserveHandoff"]:
        raise ValueError("handoff preservation is required")
    return cfg


def session_dir(sid: str) -> Path:
    if not UUID.fullmatch(sid):
        raise ValueError("invalid session id")
    return root() / sid


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("x") as f:
        json.dump(data, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def log(sid: str, event: str, **data) -> None:
    if not config()["telemetryEnabled"]:
        return
    path = session_dir(sid) / "events.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"time": time.time(), "event": event, **data}, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


def tmux(*args: str) -> str:
    return subprocess.check_output(["tmux", *args], text=True, timeout=3).strip()


def pane_identity(pane: str) -> dict:
    if not PANE.fullmatch(pane):
        raise ValueError("invalid pane")
    raw = tmux("display-message", "-p", "-t", pane, "#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_current_command}")
    fields = raw.split("|")
    if len(fields) != 4 or fields[0] != pane or not fields[1].isdigit():
        raise ValueError("pane identity unavailable")
    return dict(zip(("pane", "pid", "tty", "command"), fields))


def main_only(payload: dict) -> bool:
    return not payload.get("agent_id") and not payload.get("agent_type") and isinstance(payload.get("session_id"), str)


def orchestrator_guidance(sid: str, token: str, cfg: dict) -> str:
    command = (f"python3 {Path(__file__).resolve()} request --session {sid} --token {token} "
               "--handoff ABSOLUTE_PATH --reason SHORT_REASON --agents-reconciled --owner-input-clear")
    return (
        "Main orchestrator only. Compaction is your decision at any coherent phase checkpoint. "
        f"At approximately {cfg['reminderThresholdTokens']:,} input tokens, a one-time reminder will ask "
        "you to consider it. This is advisory, never a mandatory token cutoff; you may defer until a good "
        "stopping place. Consider whether compaction would discard useful working "
        "context, harm answer quality, or increase rather than reduce cost. "
        "Before requesting, finish and reconcile all child work, ensure no owner feedback, review, or approval "
        "is pending, and save a concise durable handoff in the project's existing planning location: current "
        "phase and goal, completed and remaining work, agent findings, branch/PR/CI state, decisions, blockers, "
        "and concrete next actions. Do not create a redundant large document. "
        f"Signal by running `{command}` through Bash; do not print `/compact` yourself. "
        "Use --agents-reconciled and --owner-input-clear only when those assertions are true. "
        "You may request compaction before or after the reminder. Then finish your turn. "
        "The coordinator checks the empty background-task registry at main Stop, enters `/compact` in this "
        "session's tmux pane, and resumes from the handoff after confirmation. Never request this from a subagent."
    )


def threshold_reminder(n: int) -> str:
    return (f"Main orchestrator context is approximately {n:,} input tokens. "
            "Consider the earliest safe checkpoint; you may defer until a good stopping place. "
            "Reconcile agents, clear pending owner decisions, and save a concise handoff before "
            "signaling with the session-specific request command. You may defer to preserve useful context.")


def active_agents(sid: str, event: str | None = None, agent_id: str | None = None) -> list[str]:
    """Track child lifecycle with a lock; an unmatched start fails closed."""
    d = session_dir(sid)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (d / "agents.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = d / "agents.json"
        agents = set(read_json(path, []))
        if event == "SubagentStart" and agent_id:
            agents.add(agent_id)
        elif event == "SubagentStop" and agent_id:
            agents.discard(agent_id)
        if event:
            atomic_json(path, sorted(agents))
        return sorted(agents)


def usage_from_transcript(path: str) -> dict | None:
    """Latest reported main conversation usage; transcript writes can lag."""
    try:
        p = Path(path).resolve(strict=True)
        if "subagents" in p.parts or not p.is_file():
            return None
        last = None
        response_time = None
        with p.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("type") != "assistant" or entry.get("isSidechain"):
                        continue
                    usage = (entry.get("message") or {}).get("usage")
                    if isinstance(usage, dict):
                        last = usage
                        response_time = entry.get("timestamp")
                except (ValueError, TypeError):
                    continue
        if not last:
            return None
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        vals = {k: last.get(k, 0) for k in keys}
        if any(type(v) is not int or v < 0 for v in vals.values()):
            return None
        vals["context_input_tokens"] = sum(vals[k] for k in keys[:3])
        vals["response_time"] = response_time
        return vals
    except OSError:
        return None


def hook(payload: dict) -> dict | None:
    cfg = config()
    if not cfg["enabled"]:
        return None
    event = payload.get("hook_event_name")
    sid = payload.get("session_id")
    if event in ("SubagentStart", "SubagentStop"):
        if isinstance(sid, str) and isinstance(payload.get("agent_id"), str):
            if read_json(session_dir(sid) / "binding.json"):
                agents = active_agents(sid, event, payload["agent_id"])
                log(sid, "agent_start" if event == "SubagentStart" else "agent_stop",
                    active_count=len(agents))
        return None
    if not main_only(payload):
        return None
    sid = payload["session_id"]
    d = session_dir(sid)
    if event == "SessionStart":
        pane = os.environ.get("TMUX_PANE", "")
        if not PANE.fullmatch(pane):
            return None
        try:
            ident = pane_identity(pane)
        except (ValueError, OSError, subprocess.SubprocessError):
            return None
        if ident["command"] != "claude":
            return None
        token = secrets.token_hex(24)
        atomic_json(d / "binding.json", {**ident, "session_id": sid, "token": token,
                                           "cwd": payload.get("cwd"), "created": time.time()})
        log(sid, "session_start", pane=pane)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                orchestrator_guidance(sid, token, cfg)}}
    binding = read_json(d / "binding.json")
    if not binding:
        return None
    if event == "UserPromptSubmit":
        req = read_json(d / "request.json")
        if req and req.get("state") in ("queued", "stop_seen"):
            req["state"] = "cancelled"
            atomic_json(d / "request.json", req)
            log(sid, "cancelled", reason="new_user_prompt")
        elif req and req.get("state") == "confirmed" and req.get("resume_state") == "pending":
            req["resume_state"] = "cancelled"
            atomic_json(d / "request.json", req)
            log(sid, "resume_cancelled", reason="new_user_prompt")
    if event in ("UserPromptSubmit", "PostToolUse", "Stop"):
        usage = usage_from_transcript(payload.get("transcript_path", ""))
        if usage:
            n = usage["context_input_tokens"]
            state = read_json(d / "usage.json", {})
            above = n >= cfg["reminderThresholdTokens"]
            if above and not state.get("above_reminder"):
                log(sid, "reminder_threshold", **usage)
            remind = (event != "Stop" and above and
                      not state.get("reminded_above_threshold", False))
            atomic_json(d / "usage.json", {"above_reminder": above, "time": time.time(),
                                             "reminded_above_threshold":
                                                 above and (remind or state.get("reminded_above_threshold", False)),
                                             **usage})
            req = read_json(d / "request.json")
            if req and req.get("state") == "confirmed" and "after" not in req:
                try:
                    response_epoch = datetime.fromisoformat(
                        usage["response_time"].replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError, AttributeError):
                    response_epoch = 0
                if response_epoch > req.get("confirmed_at", float("inf")):
                    req["after"] = n
                    atomic_json(d / "request.json", req)
                    log(sid, "after_usage", **usage)
            if remind:
                return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                        threshold_reminder(n)}}
    if event == "Stop":
        req = read_json(d / "request.json")
        if not req or req.get("state") != "queued":
            return None
        if not isinstance(payload.get("background_tasks"), list) or payload["background_tasks"]:
            log(sid, "deferred", reason="background_tasks")
            return None
        if active_agents(sid):
            log(sid, "deferred", reason="active_subagents")
            return None
        if time.time() - req.get("created", 0) > config()["requestTtlSeconds"]:
            req["state"] = "expired"
            atomic_json(d / "request.json", req)
            log(sid, "expired")
            return None
        try:
            if pane_identity(binding["pane"]) != {k: binding[k] for k in ("pane", "pid", "tty", "command")}:
                raise ValueError("pane changed")
        except (ValueError, OSError, subprocess.SubprocessError):
            log(sid, "deferred", reason="pane_identity")
            return None
        req["state"] = "stop_seen"
        atomic_json(d / "request.json", req)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "deliver", "--session", sid],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log(sid, "stop_seen")
    if event == "PostCompact":
        req = read_json(d / "request.json")
        if req and req.get("state") in ("stop_seen", "sent") and payload.get("trigger") == "manual":
            req["state"] = "confirmed"
            req["confirmed_at"] = time.time()
            req["resume_state"] = "pending" if config()["resumeAfterCompaction"] else "disabled"
            atomic_json(d / "request.json", req)
            log(sid, "confirmed", trigger=payload.get("trigger"))
            if req["resume_state"] == "pending":
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "resume", "--session", sid],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    return None


def request(args) -> int:
    cfg = config()
    if not cfg["enabled"]:
        return 2
    d = session_dir(args.session)
    binding = read_json(d / "binding.json")
    if not binding or not secrets.compare_digest(args.token, binding.get("token", "")):
        raise ValueError("unbound session")
    if os.environ.get("TMUX_PANE") != binding["pane"]:
        raise ValueError("wrong pane")
    if cfg["requireNoActiveSubagents"] and not args.agents_reconciled:
        raise ValueError("agent reconciliation required")
    if cfg["requireOwnerInputClear"] and not args.owner_input_clear:
        raise ValueError("owner input or approval clearance required")
    handoff = Path(args.handoff).resolve(strict=True)
    if any(ord(ch) < 32 for ch in str(handoff)):
        raise ValueError("unsafe handoff path")
    if not handoff.is_file() or not handoff.read_text().strip() or handoff.stat().st_size > 32768:
        raise ValueError("handoff missing, empty, or too large")
    last = read_json(d / "request.json", {})
    if last.get("state") in ("queued", "stop_seen", "sent"):
        raise ValueError("request already pending")
    if last.get("state") == "confirmed" and time.time() - last.get("created", 0) < cfg["debounceSeconds"]:
        raise ValueError("debounced")
    usage = read_json(d / "usage.json", {})
    atomic_json(d / "request.json", {"state": "queued", "created": time.time(),
                                     "handoff": str(handoff), "reason": args.reason[:200],
                                     "before": usage.get("context_input_tokens")})
    log(args.session, "requested", reason=args.reason[:200], before=usage.get("context_input_tokens"))
    print("Compaction queued for the main session's next Stop event.")
    return 0


def idle_prompt_visible(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return any(re.match(r"^❯(?:\s*$|\s+Try\s)", line) for line in lines[-4:])


def wait_for_idle_pane(binding: dict) -> None:
    expected = {k: binding[k] for k in ("pane", "pid", "tty", "command")}
    if pane_identity(binding["pane"]) != expected:
        raise ValueError("pane changed")
    for _ in range(20):
        screen = tmux("capture-pane", "-p", "-t", binding["pane"])
        if idle_prompt_visible(screen):
            time.sleep(0.8)
            again = tmux("capture-pane", "-p", "-t", binding["pane"])
            if again == screen and pane_identity(binding["pane"]) == expected:
                return
        time.sleep(0.5)
    raise ValueError("empty idle prompt not verified")


def deliver(sid: str) -> int:
    """Conservative terminal delivery. Any ambiguous screen or identity aborts."""
    d = session_dir(sid)
    req = read_json(d / "request.json")
    binding = read_json(d / "binding.json")
    if not req or req.get("state") != "stop_seen" or not binding:
        return 2
    try:
        wait_for_idle_pane(binding)
        if read_json(d / "request.json", {}).get("state") != "stop_seen":
            raise ValueError("request changed")
        # No user-provided text enters the command; the handoff is already on disk.
        req["state"] = "sent"
        atomic_json(d / "request.json", req)
        tmux("send-keys", "-t", binding["pane"], "-l", "/compact Preserve the durable handoff at " + req["handoff"])
        time.sleep(0.5)
        tmux("send-keys", "-t", binding["pane"], "Enter")
        log(sid, "sent")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        req["state"] = "failed"
        atomic_json(d / "request.json", req)
        log(sid, "failed", reason=str(e))
        return 2


def resume(sid: str) -> int:
    d = session_dir(sid)
    req = read_json(d / "request.json")
    binding = read_json(d / "binding.json")
    if not req or req.get("state") != "confirmed" or req.get("resume_state") != "pending" or not binding:
        return 2
    try:
        wait_for_idle_pane(binding)
        current = read_json(d / "request.json", {})
        if current.get("state") != "confirmed" or current.get("resume_state") != "pending":
            raise ValueError("resume changed")
        if active_agents(sid):
            raise ValueError("active subagents")
        prompt = f"Read the durable handoff at {req['handoff']} and continue the next phase. First confirm the current repository status."
        req["resume_state"] = "sent"
        atomic_json(d / "request.json", req)
        tmux("send-keys", "-t", binding["pane"], "-l", prompt)
        time.sleep(0.5)
        tmux("send-keys", "-t", binding["pane"], "Enter")
        log(sid, "resume_sent")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        req["resume_state"] = "failed"
        atomic_json(d / "request.json", req)
        log(sid, "resume_failed", reason=str(e))
        return 2


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    q = sub.add_parser("request")
    q.add_argument("--session", required=True)
    q.add_argument("--token", required=True)
    q.add_argument("--handoff", required=True)
    q.add_argument("--reason", required=True)
    q.add_argument("--agents-reconciled", action="store_true")
    q.add_argument("--owner-input-clear", action="store_true")
    x = sub.add_parser("deliver")
    x.add_argument("--session", required=True)
    x = sub.add_parser("resume")
    x.add_argument("--session", required=True)
    x = sub.add_parser("status")
    x.add_argument("--session", required=True)
    x = sub.add_parser("cancel")
    x.add_argument("--session", required=True)
    a = p.parse_args()
    try:
        if a.command == "hook":
            output = hook(json.load(sys.stdin))
            if output:
                print(json.dumps(output))
        elif a.command == "request":
            return request(a)
        elif a.command == "deliver":
            return deliver(a.session)
        elif a.command == "resume":
            return resume(a.session)
        elif a.command == "status":
            print(json.dumps({"usage": read_json(session_dir(a.session) / "usage.json"),
                              "request": read_json(session_dir(a.session) / "request.json")}, indent=2))
        elif a.command == "cancel":
            path = session_dir(a.session) / "request.json"
            req = read_json(path)
            if req and (req.get("state") in ("queued", "stop_seen") or
                        req.get("resume_state") == "pending"):
                if req.get("resume_state") == "pending":
                    req["resume_state"] = "cancelled"
                else:
                    req["state"] = "cancelled"
                atomic_json(path, req)
                log(a.session, "cancelled")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"orchestrator-compact: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
