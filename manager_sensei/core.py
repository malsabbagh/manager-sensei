"""Closed local contracts and a single transactional workflow journal.

The operator invokes components. Manager reserves and exports a handoff, retains
component receipts, and refuses a second handoff while an outcome is unresolved.
It never imports TaskAlong or ReviewSensei internals or invokes executables.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_BYTES = 1024 * 1024
MAX_ARTIFACTS = 16
ROLES = {"manager", "code", "review", "qa"}
STATES = {"proposed", "awaiting_approval", "ready", "running", "blocked",
          "needs_decision", "passed", "failed", "cancelled"}
CAPABILITIES = {"qualify_route", "propose_patch", "review_candidate", "verify_candidate", "prove_continuity"}
EFFECTS = {"read_source", "workspace_write", "local_test"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class Refusal(Exception):
    def __init__(self, code, message, exit_code=2):
        self.code, self.message, self.exit_code = code, message, exit_code
        super().__init__(message)


def require(condition, code, message, exit_code=2):
    if not condition:
        raise Refusal(code, message, exit_code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_key", "Duplicate JSON keys are refused.")
        result[key] = value
    return result


def load(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "input_too_large", "Input exceeds the 1 MiB local transport bound.")
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
    except (ValueError, UnicodeError) as exc:
        raise Refusal("invalid_json", "Input must be finite UTF-8 JSON.") from exc


def closed(value, fields, where):
    require(type(value) is dict and set(value) == set(fields), "invalid_contract",
            f"{where} requires exactly: {', '.join(fields)}.")


def string(value, where):
    require(type(value) is str and bool(value.strip()), "invalid_contract", f"{where} must be nonempty text.")


def ident(value, where):
    require(type(value) is str and bool(ID.fullmatch(value)), "invalid_identifier", f"Invalid {where}.")


def strings(value, where, *, empty=False):
    require(type(value) is list and (empty or len(value) > 0), "invalid_contract", f"{where} must be a list.")
    for item in value:
        string(item, where)
    require(len(value) == len(set(value)), "invalid_contract", f"{where} contains duplicates.")


def positive_or_unresolved(value, where):
    require(value is None or type(value) is int and value > 0,
            "invalid_budget", f"{where} must be positive or null (unresolved).")


def path_scope(value):
    require(type(value) is str and value and not value.startswith("/") and "\\" not in value,
            "invalid_scope", "Write scopes must be literal relative POSIX paths.")
    parts = value.rstrip("/").split("/")
    require(all(p and p not in {".", "..", ".git", ".agents", ".codex", ".aws", ".ssh", ".env"} for p in parts),
            "invalid_scope", "Unsafe write scope.")
    return PurePosixPath(value)


def behavior_digest(plan):
    # Source, graph, criteria, budgets, capabilities and routes all invalidate approvals.
    return sha({key: value for key, value in plan.items() if key != "approvals"})


def validate_candidate(candidate, source):
    closed(candidate, ["schema", "repository", "base_commit", "commit", "tree", "diff_sha256",
                       "dirty", "untracked_sha256", "fixture_sha256", "environment_sha256"], "candidate")
    require(candidate["schema"] == "sensei.candidate/1.0", "unsupported_schema", "Unsupported candidate schema.")
    require(candidate["repository"] == source["repository"] and candidate["base_commit"] == source["base_commit"],
            "identity_mismatch", "Candidate source differs from the approved source.", 3)
    for key in ("commit", "tree"):
        require(type(candidate[key]) is str and bool(COMMIT.fullmatch(candidate[key])), "invalid_candidate", "Candidate needs immutable commit and tree identities.")
    for key in ("diff_sha256", "untracked_sha256", "fixture_sha256", "environment_sha256"):
        require(type(candidate[key]) is str and bool(HEX.fullmatch(candidate[key])), "invalid_candidate", "Candidate needs content/environment SHA-256 identities.")
    require(type(candidate["dirty"]) is bool, "invalid_candidate", "Candidate must declare dirty state.")
    return candidate


def validate(plan):
    closed(plan, ["schema", "workflow_id", "behavior", "source", "limits", "routes", "approvals", "tasks"], "plan")
    require(plan["schema"] == "sensei.manager.plan/1.0", "unsupported_schema", "Unsupported plan schema.")
    ident(plan["workflow_id"], "workflow ID")
    b = plan["behavior"]
    closed(b, ["goal", "examples", "acceptance", "non_goals", "decision_owner"], "behavior")
    string(b["goal"], "goal"); string(b["decision_owner"], "decision owner")
    strings(b["examples"], "examples"); strings(b["non_goals"], "non-goals")
    require(type(b["acceptance"]) is dict and bool(b["acceptance"]), "invalid_contract", "Acceptance criteria are required.")
    for key, value in b["acceptance"].items():
        ident(key, "criterion ID"); string(value, "criterion")
    closed(plan["source"], ["repository", "base_commit"], "source")
    string(plan["source"]["repository"], "repository")
    require(type(plan["source"]["base_commit"]) is str and bool(COMMIT.fullmatch(plan["source"]["base_commit"])),
            "invalid_source", "An immutable base commit is required.")
    closed(plan["limits"], ["max_handoffs", "repair_limit"], "limits")
    positive_or_unresolved(plan["limits"]["max_handoffs"], "max_handoffs")
    require(plan["limits"]["repair_limit"] is None or type(plan["limits"]["repair_limit"]) is int
            and plan["limits"]["repair_limit"] == 0, "repair_unsupported",
            "Bootstrap supports repair_limit=0 or null; repairs require a separately reviewed plan.", 3)
    require(type(plan["routes"]) is dict and set(plan["routes"]) == ROLES, "invalid_contract", "Each role needs an explicit route.")
    for route in plan["routes"].values():
        closed(route, ["profile_ref", "available", "capabilities", "reason"], "route")
        string(route["profile_ref"], "profile reference")
        require(type(route["available"]) is bool, "invalid_contract", "Route availability must be boolean.")
        strings(route["capabilities"], "route capabilities", empty=True)
        string(route["reason"], "route observation")
    closed(plan["approvals"], ["behavior", "implementation"], "approvals")
    for approval in plan["approvals"].values():
        if approval is not None:
            closed(approval, ["digest", "authority_ref"], "approval")
            string(approval["authority_ref"], "existing authority reference")
            require(approval["digest"] == behavior_digest(plan), "stale_approval", "Approval does not bind this exact plan.", 3)
    tasks = plan["tasks"]
    require(type(tasks) is list and 1 <= len(tasks) <= 32, "invalid_graph", "A static graph of 1–32 tasks is required.")
    by_id = {}
    for task in tasks:
        closed(task, ["task_id", "outcome", "role", "dependencies", "inputs", "acceptance", "permitted_effects",
                      "write_paths", "max_attempts", "required_capabilities", "required_evidence", "mandatory"], "task")
        ident(task["task_id"], "task ID")
        require(task["task_id"] not in by_id, "invalid_graph", "Task IDs must be unique.")
        by_id[task["task_id"]] = task
        string(task["outcome"], "task outcome")
        require(task["role"] in ROLES, "invalid_role", "Unknown role.")
        require(type(task["mandatory"]) is bool, "invalid_contract", "mandatory must be boolean.")
        for field in ("dependencies", "inputs", "acceptance", "permitted_effects", "write_paths", "required_capabilities", "required_evidence"):
            strings(task[field], field, empty=field in {"dependencies", "write_paths"})
        require(set(task["acceptance"]) <= set(b["acceptance"]), "unknown_criterion", "Task names an unknown criterion.")
        require(set(task["permitted_effects"]) <= EFFECTS, "unsupported_effect", "Only local bootstrap effects are supported.", 3)
        require(set(task["required_capabilities"]) <= CAPABILITIES, "unsupported_capability", "Unknown required capability.", 3)
        positive_or_unresolved(task["max_attempts"], "max_attempts")
        for path in task["write_paths"]: path_scope(path)
        require(not task["write_paths"] or "workspace_write" in task["permitted_effects"], "invalid_scope", "Write paths require workspace_write.")
        require(task["role"] == "code" or not task["write_paths"], "conflicting_writer", "Only Code may own mutable source scope.", 3)
        if task["role"] == "code":
            require({"native_receipt", "candidate_manifest"} <= set(task["required_evidence"]),
                    "missing_evidence_contract", "Code requires its native receipt and exact candidate manifest.")
    seen, active = set(), set()
    def visit(task_id):
        require(task_id in by_id, "missing_dependency", f"Unknown dependency: {task_id}.")
        require(task_id not in active, "dependency_cycle", "Task dependencies contain a cycle.")
        if task_id in seen: return
        active.add(task_id)
        for dependency in by_id[task_id]["dependencies"]: visit(dependency)
        active.remove(task_id); seen.add(task_id)
    for task_id in by_id: visit(task_id)
    writers = [(t["task_id"], path_scope(p)) for t in tasks for p in t["write_paths"]]
    for index, (owner, path) in enumerate(writers):
        for other, target in writers[index + 1:]:
            require(owner == other or not (path == target or path in target.parents or target in path.parents),
                    "conflicting_writer", "One integration owner is required per overlapping write scope.", 3)
    for task in tasks:
        if task["role"] in {"review", "qa"} and any(by_id[d]["role"] == "code" for d in task["dependencies"]):
            require(task["inputs"].count("candidate") == 1, "candidate_required", "Post-Code review/QA must consume candidate.")
    require(any(t["mandatory"] and t["role"] == "code" for t in tasks)
            and any(t["mandatory"] and t["role"] == "review" and "candidate" in t["inputs"] for t in tasks)
            and any(t["mandatory"] and t["role"] == "qa" and "candidate" in t["inputs"] for t in tasks),
            "missing_independent_gate", "Mandatory candidate Review and QA tasks are required.")
    require(set(b["acceptance"]) <= {c for t in tasks if t["mandatory"] for c in t["acceptance"]},
            "uncovered_criterion", "Every behavior criterion needs a mandatory task.")
    return plan


def interview(plan):
    validate(plan)
    unresolved = []
    for kind, value in plan["approvals"].items():
        if value is None: unresolved.append({"field": f"approvals.{kind}", "question": f"Record existing {kind} authority for digest {behavior_digest(plan)}."})
    for field, value in plan["limits"].items():
        if value is None: unresolved.append({"field": f"limits.{field}", "question": f"Resolve {field}; no allowance is assumed."})
    for task in plan["tasks"]:
        if task["max_attempts"] is None: unresolved.append({"field": f"{task['task_id']}.max_attempts", "question": "Resolve the task attempt ceiling."})
        if task["role"] == "code" and not task["write_paths"]:
            unresolved.append({"field": f"{task['task_id']}.write_paths", "question": "Qualify the failure, then bind the smallest required write scope before implementation."})
    return {"behavior_digest": behavior_digest(plan), "unresolved": unresolved,
            "unavailable_routes": [role for role, route in plan["routes"].items() if not route["available"]]}


class Journal:
    """SQLite transactions serialize writers and atomically retain events/receipts."""
    def __init__(self, path, *, create=False):
        path = Path(path)
        if not create: require(path.is_file(), "workflow_missing", "Initialize a workflow first.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS workflow (id INTEGER PRIMARY KEY CHECK(id=1), plan TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS task (id TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL, active_operation TEXT, receipt TEXT);
                CREATE TABLE IF NOT EXISTS operation (id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event (seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact (sha256 TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT, 'append-only'); END;
                CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            ''')

    def close(self): self.db.close()

    def _workflow(self):
        row = self.db.execute("SELECT * FROM workflow WHERE id=1").fetchone()
        require(row is not None, "workflow_missing", "Initialize a workflow first.")
        return json.loads(row["plan"]), row["revision"]

    def _mutate(self, operation_id, payload, expected_revision, action):
        ident(operation_id, "operation ID")
        require(type(expected_revision) is int and expected_revision >= 0, "invalid_revision", "Revision must be nonnegative.")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT * FROM operation WHERE id=?", (operation_id,)).fetchone()
            if old:
                require(old["payload_digest"] == sha(payload), "operation_conflict", "Operation ID was already used with a different payload.", 3)
                result = json.loads(old["result"])
            else:
                row = self.db.execute("SELECT revision FROM workflow WHERE id=1").fetchone()
                revision = row["revision"] if row else 0
                require(revision == expected_revision, "revision_conflict", "Refresh status before changing this workflow.", 3)
                result = action()
                revision += 1
                self.db.execute("UPDATE workflow SET revision=? WHERE id=1", (revision,))
                result["revision"] = revision
                self.db.execute("INSERT INTO operation VALUES(?,?,?)", (operation_id, sha(payload), canonical(result).decode()))
                self.db.execute("INSERT INTO event(operation_id,kind,timestamp,payload) VALUES(?,?,?,?)",
                                (operation_id, payload["kind"], now(), canonical(payload).decode()))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def init(self, plan, operation_id, expected_revision=0):
        validate(plan)
        def action():
            require(self.db.execute("SELECT 1 FROM workflow").fetchone() is None, "workflow_exists", "Workflow already exists.", 3)
            self.db.execute("INSERT INTO workflow VALUES(1,?,0)", (canonical(plan).decode(),))
            for task in plan["tasks"]:
                self.db.execute("INSERT INTO task VALUES(?, 'proposed', 0, NULL, NULL)", (task["task_id"],))
            return {"workflow_id": plan["workflow_id"], "behavior_digest": behavior_digest(plan)}
        return self._mutate(operation_id, {"kind": "init", "plan": plan}, expected_revision, action)

    def _rows(self):
        return {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM task")}

    def _evidence_ok(self, receipt):
        for artifact in receipt["artifacts"]:
            stored = self.db.execute("SELECT content FROM artifact WHERE sha256=?", (artifact["sha256"],)).fetchone()
            if stored is None or len(stored[0]) != artifact["bytes"] or hashlib.sha256(stored[0]).hexdigest() != artifact["sha256"]:
                return False
        return True

    def _blockers(self, plan, task, rows):
        blockers = []
        baseline_qualification = (task["role"] == "qa" and not task["dependencies"]
                                  and task["required_capabilities"] == ["qualify_route"]
                                  and not task["write_paths"]
                                  and set(task["permitted_effects"]) <= {"read_source", "local_test"})
        # Baseline qualification supplies the evidence for the later behavior
        # decision; requiring that decision first would deadlock the pilot.
        if not baseline_qualification:
            for kind, approval in plan["approvals"].items():
                if approval is None: blockers.append(f"missing_{kind}_authority")
        if any(value is None for value in plan["limits"].values()): blockers.append("unresolved_limits")
        for dependency in task["dependencies"]:
            if rows[dependency]["state"] != "passed": blockers.append(f"dependency:{dependency}")
            elif not self._evidence_ok(json.loads(rows[dependency]["receipt"])):
                blockers.append(f"dependency_evidence:{dependency}")
        route = plan["routes"][task["role"]]
        if not route["available"]: blockers.append(f"route_unavailable:{task['role']}")
        if not set(task["required_capabilities"]) <= set(route["capabilities"]): blockers.append("required_capability_unavailable")
        row = rows[task["task_id"]]
        if task["max_attempts"] is None: blockers.append("unresolved_attempt_limit")
        elif row["attempts"] >= task["max_attempts"]: blockers.append("attempt_limit")
        used = sum(t["attempts"] for t in rows.values())
        if plan["limits"]["max_handoffs"] is not None and used >= plan["limits"]["max_handoffs"]: blockers.append("handoff_limit")
        if "candidate" in task["inputs"] and self._candidate(rows, plan) is None: blockers.append("candidate_missing")
        return blockers

    def _candidate(self, rows, plan):
        candidates = []
        for task in plan["tasks"]:
            row = rows[task["task_id"]]
            if task["role"] == "code" and row["state"] == "passed":
                candidates.append(json.loads(row["receipt"])["candidate"])
        require(len({sha(c) for c in candidates}) <= 1, "candidate_conflict", "Code tasks disagree on final candidate.", 3)
        return candidates[0] if candidates else None

    def claim(self, task_id, operation_id, expected_revision):
        def action():
            plan, _ = self._workflow(); rows = self._rows()
            tasks = {t["task_id"]: t for t in plan["tasks"]}
            require(task_id in tasks, "task_missing", "Unknown task ID.")
            row, task = rows[task_id], tasks[task_id]
            require(row["state"] == "proposed", "task_not_claimable", "Task already has an outcome or unresolved handoff; reconcile its receipt.", 3)
            blockers = self._blockers(plan, task, rows)
            require(not blockers, "task_blocked", ", ".join(blockers), 3)
            attempt = row["attempts"] + 1
            self.db.execute("UPDATE task SET state='running', attempts=?, active_operation=? WHERE id=?", (attempt, operation_id, task_id))
            return {"schema": "sensei.manager.handoff/1.0", "workflow_id": plan["workflow_id"],
                    "task_id": task_id, "attempt_id": f"{task_id}.{attempt}", "operation_id": operation_id,
                    "source": plan["source"], "candidate": self._candidate(rows, plan),
                    "behavior_digest": behavior_digest(plan), "task": task,
                    "profile_ref": plan["routes"][task["role"]]["profile_ref"],
                    "dispatch": "not_performed", "next_action": "operator_invokes_existing_component_once_then_records_receipt"}
        return self._mutate(operation_id, {"kind": "claim", "task_id": task_id}, expected_revision, action)

    def record(self, receipt, operation_id, expected_revision, *, external_observation=False):
        closed(receipt, ["schema", "workflow_id", "task_id", "handoff_operation_id", "component_run_ref", "state",
                         "candidate", "effects", "obligations", "criteria", "artifacts", "reason"], "receipt")
        require(receipt["schema"] == "sensei.manager.receipt/1.0", "unsupported_schema", "Unsupported receipt schema.")
        require(receipt["state"] in {"passed", "failed", "blocked", "needs_decision", "cancelled"}, "invalid_state", "Invalid terminal/blocked state.")
        if external_observation:
            require(receipt["state"] in {"blocked", "needs_decision"}, "observation_not_acceptance",
                    "External observation imports facts and blockers; it cannot establish acceptance or authorize dispatch.", 3)
        require(receipt["effects"] in {"confirmed", "not_sent", "unknown"}, "invalid_effects", "Record effect certainty separately.")
        strings(receipt["obligations"], "obligations", empty=True)
        string(receipt["component_run_ref"], "component run reference"); string(receipt["reason"], "reason")
        require(type(receipt["criteria"]) is dict and type(receipt["artifacts"]) is list
                and len(receipt["artifacts"]) <= MAX_ARTIFACTS, "invalid_receipt", "Criteria and at most 16 artifacts are required.")
        # Imports happen inside the transaction, after operation replay lookup.
        # A replay remains usable after the original transport files are removed.
        for artifact in receipt["artifacts"]:
            closed(artifact, ["kind", "path", "sha256", "bytes", "media_type", "producer", "created_at"], "artifact")
            for key in ("kind", "path", "media_type", "producer", "created_at"): string(artifact[key], key)
            require(type(artifact["bytes"]) is int and 0 <= artifact["bytes"] <= MAX_BYTES, "invalid_artifact", "Artifact size must be 0–1 MiB.")
            require(type(artifact["sha256"]) is str and bool(HEX.fullmatch(artifact["sha256"])), "invalid_artifact", "Artifact needs a SHA-256.")
        # Persist paths only as original transport provenance, never re-execute them.
        def action():
            plan, _ = self._workflow(); rows = self._rows()
            require(receipt["workflow_id"] == plan["workflow_id"], "identity_mismatch", "Receipt names another workflow.", 3)
            task = next((t for t in plan["tasks"] if t["task_id"] == receipt["task_id"]), None)
            require(task is not None, "task_missing", "Unknown task.")
            row = rows[receipt["task_id"]]
            if external_observation:
                previous = json.loads(row["receipt"]) if row["receipt"] else None
                unclaimed = row["state"] == "proposed" and row["active_operation"] is None
                known_not_sent = (row["state"] in {"blocked", "needs_decision"}
                                  and row["attempts"] == 0 and previous is not None
                                  and previous["effects"] == "not_sent" and not previous["obligations"])
                require(unclaimed or known_not_sent,
                        "observation_conflict", "An existing task operation must be reconciled rather than replaced.", 3)
                ident(receipt["handoff_operation_id"], "external operation ID")
                self.db.execute("UPDATE task SET active_operation=?, attempts=? WHERE id=?",
                                (receipt["handoff_operation_id"], int(receipt["effects"] != "not_sent"), receipt["task_id"]))
            else:
                require(row["active_operation"] == receipt["handoff_operation_id"] and row["state"] in {"running", "blocked", "needs_decision", "cancelled"},
                        "handoff_mismatch", "Receipt must reconcile this task's original handoff.", 3)
            criteria = receipt["criteria"]
            require(set(criteria) == set(task["acceptance"]), "coverage_mismatch", "Receipt must account for every declared criterion.", 3)
            require(all(v in {"passed", "failed", "pass", "fail", "blocked", "not_run", "skipped", "flaky"} for v in criteria.values()),
                    "invalid_criterion_result", "Unknown criterion outcome.")
            candidate = receipt["candidate"]
            if candidate is not None: validate_candidate(candidate, plan["source"])
            if receipt["state"] == "passed":
                require(receipt["effects"] != "unknown" and not receipt["obligations"], "unresolved_effects", "Passed work cannot have uncertain effects or obligations.", 3)
                require(all(value in {"passed", "pass"} for value in criteria.values()), "incomplete_acceptance", "All required criteria must pass.", 3)
                kinds = {a["kind"] for a in receipt["artifacts"]}
                require(set(task["required_evidence"]) <= kinds, "missing_evidence", "Passed task is missing required evidence.", 3)
                if task["role"] == "code": require(candidate is not None, "candidate_required", "Code must name an immutable candidate.", 3)
                if "candidate" in task["inputs"]:
                    require(candidate == self._candidate(rows, plan), "stale_candidate", "Verdict does not bind the current candidate.", 3)
            for artifact in receipt["artifacts"]:
                with Path(artifact["path"]).open("rb") as stream: content = stream.read(MAX_BYTES + 1)
                require(len(content) == artifact["bytes"] and hashlib.sha256(content).hexdigest() == artifact["sha256"],
                        "artifact_integrity", "Artifact bytes or SHA-256 do not match.", 3)
                if receipt["state"] == "passed" and artifact["kind"] == "candidate_manifest":
                    try: manifest = json.loads(content, object_pairs_hook=_pairs)
                    except (ValueError, UnicodeError) as exc:
                        raise Refusal("invalid_candidate", "Candidate manifest must be valid JSON.") from exc
                    require(manifest == candidate, "candidate_artifact_mismatch", "Retained candidate manifest differs from the receipt identity.", 3)
                self.db.execute("INSERT OR IGNORE INTO artifact VALUES(?,?)", (artifact["sha256"], content))
            self.db.execute("UPDATE task SET state=?, receipt=? WHERE id=?", (receipt["state"], canonical(receipt).decode(), receipt["task_id"]))
            return {"task_id": receipt["task_id"], "state": receipt["state"], "receipt_digest": sha(receipt)}
        return self._mutate(operation_id, {"kind": "observe_external" if external_observation else "record", "receipt": receipt}, expected_revision, action)

    def report(self):
        # A read transaction guarantees one coherent snapshot during concurrent writes.
        self.db.execute("BEGIN")
        try:
            plan, revision = self._workflow(); rows = self._rows(); tasks = []
            obligations = []
            for task in plan["tasks"]:
                row = rows[task["task_id"]]; state = row["state"]
                blockers = self._blockers(plan, task, rows) if state == "proposed" else []
                if state == "proposed":
                    state = "awaiting_approval" if any("authority" in b for b in blockers) else ("blocked" if blockers else "ready")
                receipt = json.loads(row["receipt"]) if row["receipt"] else None
                if receipt:
                    if not self._evidence_ok(receipt): blockers.append("retained_artifact_integrity")
                    if blockers and state == "passed": state = "blocked"
                unresolved = row["active_operation"] and (receipt is None or receipt["effects"] == "unknown" or bool(receipt["obligations"]))
                if unresolved:
                    obligations.append({"task_id": task["task_id"], "operation_id": row["active_operation"], "action": "reconcile_existing_component; do_not_redispatch"})
                tasks.append({"task_id": task["task_id"], "role": task["role"], "state": state,
                              "attempts": row["attempts"], "blockers": blockers, "receipt": receipt,
                              "next_action": "reconcile_existing_component" if unresolved else ("claim" if state == "ready" else "none")})
            mandatory = {t["task_id"] for t in plan["tasks"] if t["mandatory"]}
            complete = all(t["state"] == "passed" for t in tasks if t["task_id"] in mandatory) and not obligations
            return {"schema": "sensei.manager.report/1.0", "workflow_id": plan["workflow_id"], "revision": revision,
                    "behavior_digest": behavior_digest(plan), "candidate": self._candidate(rows, plan),
                    "tasks": tasks, "complete": complete, "unresolved_operations": obligations,
                    "handoffs_used": sum(t["attempts"] for t in tasks), "limits": plan["limits"],
                    "usage": {"status": "not_aggregated", "tokens": None, "cost": None},
                    "delivery": {"pr": "not_managed", "release": "not_managed", "live_recovery": "not_managed"},
                    "dispatch": "operator_driven_only"}
        finally: self.db.execute("COMMIT")

    def events(self, after=0):
        require(type(after) is int and after >= 0, "invalid_cursor", "Event cursor must be nonnegative.")
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.db.execute("SELECT * FROM event WHERE seq>? ORDER BY seq", (after,))]

    def artifact(self, digest):
        require(type(digest) is str and bool(HEX.fullmatch(digest)), "invalid_digest", "Expected SHA-256.")
        row = self.db.execute("SELECT content FROM artifact WHERE sha256=?", (digest,)).fetchone()
        require(row is not None and hashlib.sha256(row[0]).hexdigest() == digest,
                "artifact_integrity", "Retained artifact is missing or corrupt.", 3)
        return row[0]


def build_plan(request):
    """One fixed G34 graph; no dynamic planner or model invocation."""
    if request.get("schema") == "sensei.manager.plan/1.0": return validate(request)
    closed(request, ["schema", "workflow_id", "behavior", "source", "limits", "routes", "write_paths"], "request")
    require(request["schema"] == "sensei.manager.request/1.0", "unsupported_schema", "Unsupported request schema.")
    required = {"route-qualified", "result-retained", "candidate-aligned", "no-duplicate-effects"}
    require(type(request["behavior"]) is dict and type(request["behavior"].get("acceptance")) is dict
            and set(request["behavior"]["acceptance"]) == required,
            "invalid_pilot_criteria", "G34 template requires route-qualified, result-retained, candidate-aligned and no-duplicate-effects.")
    strings(request["write_paths"], "write_paths", empty=True)
    def task(task_id, role, outcome, deps, inputs, criteria, evidence, capabilities, writes=None):
        return {"task_id": task_id, "role": role, "outcome": outcome, "dependencies": deps,
                "inputs": inputs, "acceptance": criteria, "required_evidence": evidence,
                "required_capabilities": capabilities, "write_paths": writes or [],
                "permitted_effects": ["read_source", "local_test"] + (["workspace_write"] if writes else []),
                "max_attempts": 1, "mandatory": True}
    plan = {"schema": "sensei.manager.plan/1.0", "workflow_id": request["workflow_id"],
            **{k: request[k] for k in ("behavior", "source", "limits", "routes")},
            "approvals": {"behavior": None, "implementation": None}, "tasks": [
                task("qualify", "qa", "Qualify the full baseline route or name the earlier guard.", [], ["baseline", "behavior"],
                     ["route-qualified"], ["qualification_report", "effect_trace"], ["qualify_route"]),
                task("implement", "code", "Produce the bounded fix through admitted TaskAlong.", ["qualify"], ["baseline", "qualification_report"],
                     ["result-retained"], ["native_receipt", "patch", "developer_tests", "candidate_manifest"], ["propose_patch"], request["write_paths"]),
                task("review", "review", "Independently assess the frozen candidate through ReviewSensei.", ["implement"], ["candidate", "behavior"],
                     ["result-retained", "candidate-aligned"], ["native_receipt", "assessment"], ["review_candidate"]),
                task("verify", "qa", "Run frozen acceptance against the same candidate.", ["implement"], ["candidate", "qualification_report", "behavior"],
                     ["result-retained", "candidate-aligned", "no-duplicate-effects"], ["native_receipt", "check_receipts", "effect_trace"], ["verify_candidate"]),
                task("continuity", "manager", "Prove fresh-process resume and report retained component outcomes.", ["review", "verify"], ["candidate", "component_receipts"],
                     ["candidate-aligned", "no-duplicate-effects"], ["resume_receipt"], ["prove_continuity"])]}
    return validate(plan)
