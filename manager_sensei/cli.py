"""Small operator-facing JSON CLI over the deterministic journal."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .core import (Journal, MAX_ARTIFACTS, MAX_BYTES, Refusal, behavior_digest,
                   build_plan, canonical, interview, load, require, validate)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal("invalid_cli", "Invalid syntax; use --help.")


def parser():
    root = Parser(prog="manager-sensei", description="Local operator-driven coordinator; automatic component dispatch is unsupported.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--json", action="store_true")
    sub = root.add_subparsers(dest="command", required=True)
    def command(name):
        cmd = sub.add_parser(name); cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return cmd
    command("capabilities")
    p = command("plan"); p.add_argument("--input", required=True)
    p.add_argument("--behavior-authority-ref"); p.add_argument("--implementation-authority-ref")
    for name in ("validate", "interview"):
        p = command(name); p.add_argument("--plan", required=True)
    p = command("init"); p.add_argument("--plan", required=True); p.add_argument("--db", required=True)
    p.add_argument("--operation-id", required=True); p.add_argument("--expected-revision", type=int, default=0)
    p = command("claim"); p.add_argument("--db", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--operation-id", required=True); p.add_argument("--expected-revision", type=int, required=True)
    for name in ("record", "observe"):
        p = command(name); p.add_argument("--db", required=True); p.add_argument("--receipt", required=True)
        p.add_argument("--operation-id", required=True); p.add_argument("--expected-revision", type=int, required=True)
    for name in ("status", "resume", "report", "gate", "events", "export-artifact"):
        p = command(name); p.add_argument("--db", required=True)
        if name == "events": p.add_argument("--after-seq", type=int, default=0)
        if name == "export-artifact": p.add_argument("--sha256", required=True); p.add_argument("--output", required=True)
    return root


def capabilities():
    return {"schema": "sensei.manager.capabilities/1.0", "version": __version__,
            "operations": ["plan", "validate", "interview", "init", "claim", "record", "observe", "status", "resume", "report", "gate", "events", "export-artifact"],
            "schemas": ["sensei.manager.plan/1.0", "sensei.manager.handoff/1.0", "sensei.manager.receipt/1.0", "sensei.candidate/1.0"],
            "automatic_dispatch": False, "native_status_reconciliation": False,
            "resume": "read retained state and require operator reconciliation of unresolved component operations",
            "budget_enforcement": {"handoff_count": "enforced", "provider_cost": "unsupported", "tokens": "unsupported", "provider_time": "unsupported"},
            "limits": {"input_bytes": MAX_BYTES, "artifact_bytes": MAX_BYTES, "artifacts_per_receipt": MAX_ARTIFACTS, "tasks": 32},
            "repairs": "new separately reviewed plan required", "criterion_spelling": "passed (legacy pass accepted without rewriting native receipts)",
            "effects": ["local_journal_write", "explicit_artifact_import_export"],
            "unsupported": ["provider_execution", "provider_fallback", "remote_cancel", "release", "PR_approval", "live_recovery"]}


def execute(args):
    cmd = args.command
    if cmd == "capabilities": return capabilities(), 0
    if cmd == "plan":
        plan = build_plan(load(args.input))
        for kind in ("behavior", "implementation"):
            ref = getattr(args, kind + "_authority_ref")
            if ref: plan["approvals"][kind] = {"digest": behavior_digest(plan), "authority_ref": ref}
        return validate(plan), 0
    if cmd in {"validate", "interview"}:
        plan = validate(load(args.plan))
        return interview(plan) | {"valid": True}, 0
    journal = Journal(args.db, create=cmd == "init")
    try:
        if cmd == "init": return journal.init(load(args.plan), args.operation_id, args.expected_revision), 0
        if cmd == "claim": return journal.claim(args.task_id, args.operation_id, args.expected_revision), 0
        if cmd in {"record", "observe"}:
            return journal.record(load(args.receipt), args.operation_id, args.expected_revision, external_observation=cmd == "observe"), 0
        if cmd == "events": return {"events": journal.events(args.after_seq)}, 0
        if cmd == "export-artifact":
            content = journal.artifact(args.sha256)
            with Path(args.output).open("xb") as stream: stream.write(content)
            return {"sha256": args.sha256, "bytes": len(content), "output": args.output}, 0
        report = journal.report()
        return report, 3 if cmd == "gate" and not report["complete"] else 0
    finally: journal.close()


def main(argv=None):
    cmd, exit_code = "unknown", 0
    try:
        args = parser().parse_args(argv)
        cmd = args.command
        data, exit_code = execute(args)
        result = {"schema": "sensei.manager.command/1.0", "command": cmd, "ok": True, "data": data}
    except Refusal as exc:
        exit_code = exc.exit_code
        result = {"schema": "sensei.manager.command/1.0", "command": cmd, "ok": False,
                  "error": {"code": exc.code, "message": exc.message}}
    except (OSError, sqlite3.Error) as exc:
        exit_code = 4
        result = {"schema": "sensei.manager.command/1.0", "command": cmd, "ok": False,
                  "error": {"code": "local_operation_failed", "message": "Local I/O failed; preserve state and inspect inputs before retrying."}}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        exit_code = 2
        result = {"schema": "sensei.manager.command/1.0", "command": cmd, "ok": False,
                  "error": {"code": "invalid_contract", "message": "Malformed or unsupported input shape."}}
    print(canonical(result).decode())
    return exit_code
