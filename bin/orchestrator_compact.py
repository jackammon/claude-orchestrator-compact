#!/usr/bin/env python3
"""Fail-closed, main-session-only Claude Code compaction coordinator."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

DEFAULTS = {"enabled": True, "softThresholdTokens": 200000,
            "preferredCompactionTokens": 300000, "escalationThresholdTokens": 400000,
            "requestTtlSeconds": 300, "debounceSeconds": 300,
            "requirePhaseCheckpoint": True, "requireNoActiveSubagents": True,
            "preserveHandoff": True, "resumeAfterCompaction": False,
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
    for k in ("softThresholdTokens", "preferredCompactionTokens", "escalationThresholdTokens"):
        if type(cfg[k]) is not int or cfg[k] < 0:
            raise ValueError(f"invalid {k}")
    if not cfg["softThresholdTokens"] <= cfg["preferredCompactionTokens"] <= cfg["escalationThresholdTokens"]:
        raise ValueError("threshold order")
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


def usage_from_transcript(path: str) -> dict | None:
    """Latest reported main conversation usage; transcript writes can lag."""
    try:
        p = Path(path).resolve(strict=True)
        if "subagents" in p.parts or not p.is_file():
            return None
        last = None
        with p.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("type") != "assistant" or entry.get("isSidechain"):
                        continue
                    usage = (entry.get("message") or {}).get("usage")
                    if isinstance(usage, dict):
                        last = usage
                except (ValueError, TypeError):
                    continue
        if not last:
            return None
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        vals = {k: last.get(k, 0) for k in keys}
        if any(type(v) is not int or v < 0 for v in vals.values()):
            return None
        vals["context_input_tokens"] = sum(vals[k] for k in keys[:3])
        return vals
    except OSError:
        return None


def hook(payload: dict) -> dict | None:
    if not config()["enabled"] or not main_only(payload):
        return None
    sid = payload["session_id"]
    d = session_dir(sid)
    event = payload.get("hook_event_name")
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
                f"Main orchestrator only: at phase checkpoints, inspect context reminders. If compaction helps, save a durable handoff and call `python3 {Path(__file__).resolve()} request --session {sid} --token {token} --handoff ABSOLUTE_PATH --reason SHORT_REASON --agents-reconciled`. Never do this from a subagent. This coordinator requires an empty background task registry at Stop."}}
    binding = read_json(d / "binding.json")
    if not binding:
        return None
    if event in ("UserPromptSubmit", "PostToolUse", "Stop"):
        usage = usage_from_transcript(payload.get("transcript_path", ""))
        if usage:
            n = usage["context_input_tokens"]
            cfg = config()
            tier = "escalated" if n >= cfg["escalationThresholdTokens"] else "preferred" if n >= cfg["preferredCompactionTokens"] else "soft" if n >= cfg["softThresholdTokens"] else "normal"
            state = read_json(d / "usage.json", {})
            if state.get("tier") != tier:
                log(sid, "threshold", tier=tier, **usage)
            remind = event != "Stop" and tier != "normal" and state.get("reminded_tier") != tier
            atomic_json(d / "usage.json", {"tier": tier, "time": time.time(),
                                             "reminded_tier": tier if remind else state.get("reminded_tier"),
                                             **usage})
            if remind:
                return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                        f"Main orchestrator context is approximately {n:,} input tokens ({tier}). Consider compaction at a coherent checkpoint after agents finish. You may defer it to preserve the current phase. Save a handoff first."}}
    if event == "Stop":
        req = read_json(d / "request.json")
        if not req or req.get("state") != "queued":
            return None
        if not isinstance(payload.get("background_tasks"), list) or payload["background_tasks"]:
            log(sid, "deferred", reason="background_tasks")
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
        if req and req.get("state") in ("stop_seen", "sent"):
            req["state"] = "confirmed"
            atomic_json(d / "request.json", req)
            log(sid, "confirmed", trigger=payload.get("trigger"))
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
    if usage.get("tier", "normal") == "normal" and cfg["requirePhaseCheckpoint"] and not args.force:
        raise ValueError("below soft threshold; use --force for a deliberate phase checkpoint")
    atomic_json(d / "request.json", {"state": "queued", "created": time.time(),
                                     "handoff": str(handoff), "reason": args.reason[:200],
                                     "before": usage.get("context_input_tokens")})
    log(args.session, "requested", reason=args.reason[:200], before=usage.get("context_input_tokens"))
    print("Compaction queued for the main session's next Stop event.")
    return 0


def idle_prompt_visible(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return any(re.match(r"^❯(?:\s*$|\s+Try\s)", line) for line in lines[-4:])


def deliver(sid: str) -> int:
    """Conservative terminal delivery. Any ambiguous screen or identity aborts."""
    d = session_dir(sid)
    req = read_json(d / "request.json")
    binding = read_json(d / "binding.json")
    if not req or req.get("state") != "stop_seen" or not binding:
        return 2
    try:
        if pane_identity(binding["pane"]) != {k: binding[k] for k in ("pane", "pid", "tty", "command")}:
            raise ValueError("pane changed")
        # The interactive prompt is visible only after the Stop hook exits.
        # Require a stable, empty Claude prompt twice. This is a best-effort UI check.
        for _ in range(20):
            screen = tmux("capture-pane", "-p", "-t", binding["pane"])
            if idle_prompt_visible(screen):
                time.sleep(0.8)
                again = tmux("capture-pane", "-p", "-t", binding["pane"])
                if again == screen and pane_identity(binding["pane"]) == {k: binding[k] for k in ("pane", "pid", "tty", "command")}:
                    break
            time.sleep(0.5)
        else:
            raise ValueError("empty idle prompt not verified")
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
    q.add_argument("--force", action="store_true")
    x = sub.add_parser("deliver")
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
        elif a.command == "status":
            print(json.dumps({"usage": read_json(session_dir(a.session) / "usage.json"),
                              "request": read_json(session_dir(a.session) / "request.json")}, indent=2))
        elif a.command == "cancel":
            path = session_dir(a.session) / "request.json"
            req = read_json(path)
            if req and req.get("state") in ("queued", "stop_seen"):
                req["state"] = "cancelled"
                atomic_json(path, req)
                log(a.session, "cancelled")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"orchestrator-compact: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
