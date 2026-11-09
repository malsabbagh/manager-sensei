"""Bootstrap conformance through process boundaries and durable state."""
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from manager_sensei.core import (Journal, Refusal, behavior_digest, build_plan,
                                canonical, load, sha, validate)

ROOT = Path(__file__).resolve().parents[1]


def request():
    return {
        "schema": "sensei.manager.request/1.0", "workflow_id": "bootstrap-conformance",
        "behavior": {"goal": "Retain complete results or refuse before dependent effects.",
                     "examples": ["Small result settles; oversized valid result refuses before an effect."],
                     "acceptance": {"route-qualified": "Name the exact failing boundary.",
                                    "result-retained": "No silent truncation or omitted digest.",
                                    "candidate-aligned": "Review and QA bind the same full manifest.",
                                    "no-duplicate-effects": "Resume reuses retained completed work."},
                     "non_goals": ["No live recovery, release or larger budgets."], "decision_owner": "bootstrap fixture"},
        "source": {"repository": "fixture:review-sensei", "base_commit": "a" * 40},
        "limits": {"max_handoffs": 5, "repair_limit": 0},
        "routes": {role: {"profile_ref": "fixture:" + role, "available": True,
                          "capabilities": caps, "reason": "Deterministic fixture only; not a live route."}
                   for role, caps in {"manager": ["prove_continuity"], "code": ["propose_patch"],
                                      "review": ["review_candidate"], "qa": ["qualify_route", "verify_candidate"]}.items()},
        "write_paths": ["src/result.py", "tests/test_result.py"]}


def approved_plan():
    plan = build_plan(request())
    for kind in plan["approvals"]:
        plan["approvals"][kind] = {"digest": behavior_digest(plan), "authority_ref": "fixture:existing-" + kind + "-authority"}
    return plan


def candidate():
    return {"schema": "sensei.candidate/1.0", "repository": "fixture:review-sensei", "base_commit": "a" * 40,
            "commit": "b" * 40, "tree": "c" * 40, "diff_sha256": "1" * 64, "dirty": True,
            "untracked_sha256": "2" * 64, "fixture_sha256": "3" * 64, "environment_sha256": "4" * 64}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "workflow.sqlite3"
        self.plan = approved_plan()
        self.save("plan.json", self.plan)
        self.cli("init", "--plan", str(self.root / "plan.json"), "--operation-id", "init", "--db", str(self.db))

    def tearDown(self): self.temp.cleanup()

    def save(self, name, value):
        path = self.root / name; path.write_bytes(canonical(value)); return str(path)

    def cli(self, *args, expected=0):
        env = os.environ | {"PYTHONPATH": str(ROOT)}
        proc = subprocess.run([sys.executable, "-m", "manager_sensei", *args, "--json"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    def report(self): return self.cli("report", "--db", str(self.db))["data"]

    def claim(self, task):
        return self.cli("claim", "--db", str(self.db), "--task-id", task, "--operation-id", "handoff-" + task,
                        "--expected-revision", str(self.report()["revision"]))["data"]

    def receipt(self, task, **changes):
        spec = next(t for t in self.plan["tasks"] if t["task_id"] == task)
        artifacts = []
        for kind in spec["required_evidence"]:
            path = self.root / f"{task}-{kind}.json"
            path.write_bytes(canonical(candidate() if kind == "candidate_manifest" else {"bootstrap": True, "kind": kind, "task": task}))
            raw = path.read_bytes()
            artifacts.append({"kind": kind, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                              "bytes": len(raw), "media_type": "application/json", "producer": "fixture:" + spec["role"],
                              "created_at": "2026-11-07T00:00:00Z"})
        result = {"schema": "sensei.manager.receipt/1.0", "workflow_id": self.plan["workflow_id"], "task_id": task,
                  "handoff_operation_id": "handoff-" + task, "component_run_ref": "fixture:run-" + task,
                  "state": "passed", "candidate": None if task == "qualify" else candidate(), "effects": "confirmed",
                  "obligations": [], "criteria": {c: "passed" for c in spec["acceptance"]}, "artifacts": artifacts,
                  "reason": "Fixture attestation for bootstrap conformance; no real component invoked."}
        result.update(changes)
        return result

    def record(self, receipt, *, op=None, expected=0):
        task = receipt["task_id"]
        path = self.save("receipt.json", receipt)
        return self.cli("record", "--db", str(self.db), "--receipt", path, "--operation-id", op or "record-" + task,
                        "--expected-revision", str(self.report()["revision"]), expected=expected)

    def pass_task(self, task):
        self.claim(task); return self.record(self.receipt(task))

    def test_fresh_process_workflow_gate_and_retained_events(self):
        self.cli("gate", "--db", str(self.db), expected=3)
        for task in ("qualify", "implement", "review", "verify", "continuity"):
            self.pass_task(task)
        report = self.cli("gate", "--db", str(self.db))["data"]
        self.assertTrue(report["complete"])
        self.assertEqual(report["candidate"], candidate())
        self.assertEqual(report["usage"]["cost"], None)
        self.assertEqual(self.cli("resume", "--db", str(self.db))["data"], report)
        events = self.cli("events", "--db", str(self.db), "--after-seq", "9")["data"]["events"]
        self.assertEqual([e["seq"] for e in events], [10, 11])
        self.assertEqual(report["handoffs_used"], 5)

    def test_unknown_effect_survives_cancel_resume_and_no_redispatch(self):
        self.claim("qualify")
        receipt = self.receipt("qualify", state="cancelled", effects="unknown", obligations=["reconcile remote work"],
                               criteria={"route-qualified": "not_run"})
        self.record(receipt)
        resumed = self.cli("resume", "--db", str(self.db))["data"]
        self.assertFalse(resumed["complete"])
        self.assertEqual(len(resumed["unresolved_operations"]), 1)
        refused = self.cli("claim", "--db", str(self.db), "--task-id", "qualify", "--operation-id", "second-dispatch",
                           "--expected-revision", str(resumed["revision"]), expected=3)
        self.assertEqual(refused["error"]["code"], "task_not_claimable")
        # A confirmed receipt reconciles the original handoff without a second attempt.
        self.record(self.receipt("qualify"), op="reconcile-qualify")
        self.assertEqual(self.report()["unresolved_operations"], [])
        self.assertEqual(self.report()["handoffs_used"], 1)

    def test_same_operation_reuses_receipt_and_conflicting_payload_refuses(self):
        first = self.claim("qualify")
        duplicate = self.cli("claim", "--db", str(self.db), "--task-id", "qualify", "--operation-id", "handoff-qualify",
                             "--expected-revision", "0")["data"]
        self.assertEqual(duplicate, first)
        result = self.cli("claim", "--db", str(self.db), "--task-id", "implement", "--operation-id", "handoff-qualify",
                          "--expected-revision", "0", expected=3)
        self.assertEqual(result["error"]["code"], "operation_conflict")
        self.assertEqual(self.report()["handoffs_used"], 1)

    def test_receipt_replay_does_not_need_original_files(self):
        self.claim("qualify"); receipt = self.receipt("qualify"); first = self.record(receipt)
        for artifact in receipt["artifacts"]: Path(artifact["path"]).unlink()
        self.assertEqual(self.record(receipt), first)
        digest = receipt["artifacts"][0]["sha256"]
        out = self.root / "export.json"
        self.cli("export-artifact", "--db", str(self.db), "--sha256", digest, "--output", str(out))
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), digest)

    def test_stale_dirty_or_environment_manifest_refuses(self):
        self.pass_task("qualify"); self.pass_task("implement"); self.claim("verify")
        for field, value in [("untracked_sha256", "f" * 64), ("dirty", False), ("environment_sha256", "e" * 64)]:
            c = candidate(); c[field] = value
            result = self.record(self.receipt("verify", candidate=c), op="stale-" + field, expected=3)
            self.assertEqual(result["error"]["code"], "stale_candidate")

    def test_missing_or_tampered_evidence_never_passes(self):
        self.claim("qualify")
        receipt = self.receipt("qualify", artifacts=[])
        self.assertEqual(self.record(receipt, expected=3)["error"]["code"], "missing_evidence")
        receipt = self.receipt("qualify")
        Path(receipt["artifacts"][0]["path"]).write_text("tampered")
        self.assertEqual(self.record(receipt, op="tampered", expected=3)["error"]["code"], "artifact_integrity")
        self.assertEqual(self.report()["tasks"][0]["state"], "running")

    def test_corrupt_retained_artifact_blocks_fresh_gate(self):
        for task in ("qualify", "implement", "review", "verify", "continuity"): self.pass_task(task)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE artifact SET content=?", (b"corrupted",)); db.commit()
        report = self.cli("gate", "--db", str(self.db), expected=3)["data"]
        self.assertFalse(report["complete"])
        self.assertIn("retained_artifact_integrity", report["tasks"][0]["blockers"])

    def test_legacy_pass_is_accepted_and_native_receipt_is_preserved(self):
        self.claim("qualify"); receipt = self.receipt("qualify", criteria={"route-qualified": "pass"})
        self.record(receipt)
        self.assertEqual(self.report()["tasks"][0]["receipt"], receipt)

    def test_incomplete_coverage_and_unknown_usage_effects_block_acceptance(self):
        self.claim("qualify")
        for outcome in ("failed", "blocked", "skipped", "flaky", "not_run"):
            r = self.receipt("qualify", criteria={"route-qualified": outcome})
            self.assertEqual(self.record(r, op="outcome-" + outcome, expected=3)["error"]["code"], "incomplete_acceptance")
        self.assertEqual(self.record(self.receipt("qualify", effects="unknown"), op="unknown", expected=3)["error"]["code"], "unresolved_effects")

    def test_concurrent_revision_only_one_handoff_commits(self):
        self.pass_task("qualify"); self.pass_task("implement")
        rev = self.report()["revision"]
        env = os.environ | {"PYTHONPATH": str(ROOT)}
        processes = [subprocess.Popen([sys.executable, "-m", "manager_sensei", "claim", "--db", str(self.db),
                                      "--task-id", task, "--operation-id", "parallel-" + task, "--expected-revision", str(rev)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                     for task in ("review", "verify")]
        results = [p.communicate(timeout=10) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 3], results)
        self.assertEqual(self.report()["handoffs_used"], 3)

    def test_event_journal_is_append_only(self):
        with closing(sqlite3.connect(self.db)) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM event")

    def test_corrupt_dependency_cannot_authorize_later_handoff(self):
        self.pass_task("qualify")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE artifact SET content=?", (b"corrupted",)); db.commit()
        result = self.cli("claim", "--db", str(self.db), "--task-id", "implement", "--operation-id", "after-corruption",
                          "--expected-revision", str(self.report()["revision"]), expected=3)
        self.assertEqual(result["error"]["code"], "task_blocked")

    def test_candidate_artifact_must_match_code_receipt(self):
        self.pass_task("qualify"); self.claim("implement")
        changed = candidate(); changed["diff_sha256"] = "e" * 64
        result = self.record(self.receipt("implement", candidate=changed), expected=3)
        self.assertEqual(result["error"]["code"], "candidate_artifact_mismatch")

    def test_handoff_budget_and_missing_route_remain_blocked(self):
        for name in ("budget", "route", "authority"):
            plan = approved_plan(); plan["workflow_id"] = name
            if name == "budget": plan["limits"]["max_handoffs"] = None
            if name == "route": plan["routes"]["qa"]["available"] = False
            # Rebind already-existing fixture authority after changing the fixture.
            for kind in plan["approvals"]:
                plan["approvals"][kind] = {"digest": behavior_digest(plan), "authority_ref": "fixture:existing-authority"}
            if name == "authority": plan["approvals"]["implementation"] = None
            path = self.save(name + ".json", plan); db = str(self.root / (name + ".sqlite3"))
            self.cli("init", "--db", db, "--plan", path, "--operation-id", "init")
            if name == "authority":
                # Local baseline qualification precedes product behavior approval.
                self.cli("claim", "--db", db, "--task-id", "qualify", "--operation-id", "qualification", "--expected-revision", "1")
                result=self.cli("report", "--db", db)["data"]
                self.assertEqual(result["tasks"][0]["state"], "running")
                self.assertIn("missing_implementation_authority",result["tasks"][1]["blockers"])
                continue
            result = self.cli("claim", "--db", db, "--task-id", "qualify", "--operation-id", "blocked", "--expected-revision", "1", expected=3)
            self.assertEqual(result["error"]["code"], "task_blocked")
            self.assertEqual(self.cli("report", "--db", db)["data"]["handoffs_used"], 0)

    def test_external_observation_preserves_native_uncertainty_without_dispatch(self):
        receipt = self.receipt("qualify", state="blocked", effects="unknown",
                               obligations=["native remote usage remains unknown"], criteria={"route-qualified": "not_run"})
        path = self.save("observation.json", receipt)
        first = self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "observe-native", "--expected-revision", "1")
        replay = self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "observe-native", "--expected-revision", "0")
        self.assertEqual(first, replay)
        report = self.report()
        self.assertEqual(report["tasks"][0]["receipt"], receipt)
        self.assertEqual(report["handoffs_used"], 1)
        self.assertEqual(len(report["unresolved_operations"]), 1)
        self.assertFalse(report["complete"])

    def test_pre_dispatch_blocker_reserves_no_handoff_and_cannot_be_greenwashed(self):
        receipt = self.receipt("qualify", state="blocked", effects="not_sent", criteria={"route-qualified": "not_run"})
        path = self.save("not-sent.json", receipt)
        self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "not-sent", "--expected-revision", "1")
        self.assertEqual(self.report()["handoffs_used"], 0)
        self.assertEqual(self.report()["unresolved_operations"], [])
        accepted = self.save("not-acceptance.json", self.receipt("implement"))
        r = self.cli("observe", "--db", str(self.db), "--receipt", accepted, "--operation-id", "false-acceptance", "--expected-revision", "2", expected=3)
        self.assertEqual(r["error"]["code"], "observation_not_acceptance")

    def test_known_not_sent_observation_can_import_the_actual_external_run(self):
        before = self.receipt("qualify", state="needs_decision", effects="not_sent", criteria={"route-qualified": "not_run"})
        path = self.save("not-sent-before-approval.json", before)
        self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "pre-dispatch", "--expected-revision", "1")
        after = self.receipt("qualify", state="blocked", effects="unknown", obligations=["retain native accounting"],
                             criteria={"route-qualified": "not_run"}, handoff_operation_id="actual-native-run-1")
        path = self.save("actual-run.json", after)
        self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "observe-actual", "--expected-revision", "2")
        report = self.report()
        self.assertEqual(report["handoffs_used"], 1)
        self.assertEqual(report["tasks"][0]["receipt"], after)
        events = self.cli("events", "--db", str(self.db))["data"]["events"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["payload"]["receipt"], before)
        self.assertEqual(events[2]["payload"]["receipt"], after)
        # A second native run cannot replace this unknown operation.
        after["handoff_operation_id"] = "different-native-run"
        path = self.save("must-not-replace.json", after)
        result = self.cli("observe", "--db", str(self.db), "--receipt", path, "--operation-id", "overwrite", "--expected-revision", "3", expected=3)
        self.assertEqual(result["error"]["code"], "observation_conflict")


class ContractTests(unittest.TestCase):
    def test_invalid_graphs_authority_capabilities_and_scope(self):
        cases = [
            (lambda p: p["tasks"][0]["dependencies"].append("absent"), "missing_dependency"),
            (lambda p: p["tasks"][0]["dependencies"].append("continuity"), "dependency_cycle"),
            (lambda p: p["tasks"][1]["required_capabilities"].append("pay_for_provider"), "unsupported_capability"),
            (lambda p: p["tasks"][1]["write_paths"].append("../escape"), "invalid_scope"),
            (lambda p: p["limits"].update(max_handoffs=20), "stale_approval"),
        ]
        for change, code in cases:
            with self.subTest(code=code):
                plan = approved_plan(); change(plan)
                # Graph errors must be examined without earlier stale approvals.
                if code != "stale_approval": plan["approvals"] = {"behavior": None, "implementation": None}
                with self.assertRaises(Refusal) as caught: validate(plan)
                self.assertEqual(caught.exception.code, code)

    def test_overlapping_writers_rejected(self):
        plan = build_plan(request()); second = copy.deepcopy(plan["tasks"][1]); second["task_id"] = "other-code"
        second["write_paths"] = ["src/"]; plan["tasks"].append(second)
        with self.assertRaises(Refusal) as caught: validate(plan)
        self.assertEqual(caught.exception.code, "conflicting_writer")

    def test_duplicate_keys_nonfinite_and_oversize_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            for raw, expected in [(b'{"x":1,"x":2}', "duplicate_key"), (b'{"x":NaN}', "invalid_json"), (b" " * (1048576 + 1), "input_too_large")]:
                path.write_bytes(raw)
                with self.assertRaises(Refusal) as caught: load(path)
                self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__": unittest.main()
