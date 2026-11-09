# ManagerSensei bootstrap
Part of the four-CLI Sensei family: CodeSensei implements, ReviewSensei reviews,
ManagerSensei coordinates, and QASensei independently verifies. CodeSensei is the
new name for TaskAlong; existing `taskalong` command, import and state identities
remain compatibility contracts until an explicit migration is qualified.


A working, local, operator-driven CLI for one static G34 workflow. It turns a
bounded request into actionable tasks, validates the graph, reserves handoffs,
retains component run pointers and exact evidence bytes, and reports the next
action after a fresh-process restart. Python 3.11+; no runtime dependencies.

This is bootstrap scaffolding. Automatic CodeSensei/ReviewSensei/QASensei invocation,
native status reconciliation, provider cost/token enforcement, dynamic repair,
and live-provider qualification are **unsupported**. `claim` exports a handoff;
the operator invokes the existing component and uses `record` to retain its
receipt. `resume` reads retained state and identifies operations that need native
reconciliation. It does not contact a component or silently retry it.

## Run without installation

From this directory:

```sh
python3 -m manager_sensei capabilities --json
python3 -m manager_sensei plan --input examples/g34-request.json > /tmp/g34-plan-envelope.json
python3 -c 'import json; p=json.load(open("/tmp/g34-plan-envelope.json")); json.dump(p["data"],open("/tmp/g34-plan.json","w"))'
python3 -m manager_sensei interview --plan /tmp/g34-plan.json
python3 -m manager_sensei init --plan /tmp/g34-plan.json --db /tmp/g34-manager.sqlite3 --operation-id g34-init
python3 -m manager_sensei status --db /tmp/g34-manager.sqlite3
python3 -m manager_sensei resume --db /tmp/g34-manager.sqlite3
python3 -m manager_sensei gate --db /tmp/g34-manager.sqlite3
```

The G34 example deliberately reports missing qualified routes/limits and an
unapproved exact G34 contract. Existing authorization to **build this bootstrap**
is distinct from approval of an as-yet-unqualified G34 product behavior. Its
`max_attempts=1` template ceiling is a conservative local handoff limit, not an
inference or spending grant. The example makes no provider calls.

All commands emit one JSON envelope on stdout (also without `--json`). Request
files contain the raw `data` object rather than a prior command envelope. Exit 0
means protocol retrieval/mutation succeeded, including reports of failed tasks;
2 means invalid input; 3 means policy/state refusal; 4 means local I/O failed.
`gate` is the explicit CI mapping: exit 3 until every mandatory declared task is
passed, retained evidence is intact, and no operation/effect obligation remains.

## Public commands and contracts

`plan --input request.json` emits a fixed dependency graph:

```text
qualify (QA) -> implement (TaskAlong) -> review (ReviewSensei) --+
                                    -> verify (QA) ----------+-> continuity (Manager)
```

`validate --plan` checks source, task criteria, approval digests, known
capabilities, graph cycles, dependencies, effect scopes and conflicting writers.
It allows unresolved decisions to remain explicit. `interview --plan` lists only
the missing authority/limit decisions and unavailable route observations.

The invoking trusted operator can record **already-existing** authority with
`plan --behavior-authority-ref REF --implementation-authority-ref REF`.
References assert provenance; they do not authenticate a human, grant privileges,
or bypass the receiving component's policy. Never use this to invent a missing
decision. Every effective plan field except the approval records contributes to
the approval digest; source, budget, criteria, graph or route changes invalidate
those records. There is no in-place plan editing: start a new workflow for a
changed contract, retaining the previous database and unresolved operations.

```sh
python3 -m manager_sensei claim --db workflow.sqlite3 --task-id qualify \
  --operation-id qualify-handoff-1 --expected-revision 1
python3 -m manager_sensei record --db workflow.sqlite3 --receipt receipt.json \
  --operation-id retain-qualify-1 --expected-revision 2
```

`claim` atomically reserves one handoff, marks the task running and returns its
stable attempt/operation identity and permitted scope. It dispatches nothing.
The role's profile must be explicitly available and capable. Handoff counts are
hard local reservations; model tokens, costs and time remain component-owned.
No allowance is assumed when limits are null. Cancellation with an unknown
effect retains an unresolved operation; a new claim is refused. Reconcile by
recording a confirmed receipt for the **original** handoff, never by creating
another execution. Passed/failed tasks cannot be reopened as fresh attempts.
Local baseline QA qualification does not wait for the later G34 product-behavior
decision: that evidence is needed to make the decision. It still requires an
available local route and a resolved handoff limit. Product implementation and
candidate assessment require the exact behavior and implementation references.
Repair is intentionally unsupported here; preserve history and negotiate the
next bounded plan with the parent rather than inventing a repair budget.

`record` takes `sensei.manager.receipt/1.0`. It includes workflow/task/handoff
identity, the native `component_run_ref`, state, full `candidate`, effect
certainty, obligations, per-criterion results, artifact records and a reason.
Keep original native receipts as artifacts: normalized criteria never replace
them. `passed` is canonical; legacy `pass` is accepted **without rewriting the
original receipt**. Missing, skipped, flaky, blocked and not-run criteria cannot
become a passed task. An uncertain effect or outstanding obligation cannot pass.

`observe --db DB --receipt RECEIPT --operation-id ID --expected-revision N`
imports a pre-existing native operation or a known pre-dispatch blocker for an
unclaimed task, or a task whose previous observation is confirmed `not_sent`
with zero attempts and no obligations. It only admits `blocked`/`needs_decision`, preserves exact native
artifacts and cannot authorize dispatch or establish acceptance. A `not_sent`
observation consumes no handoff; an existing submitted operation preserves one
prior attempt in aggregate accounting. Existing uncertain operations cannot be replaced.
Use `record` to reconcile that same operation later. Events explicitly distinguish
`observe_external` from Manager's own `claim`; importing history never means that
Manager drove an earlier execution.

The current receipt is an **operator attestation**, not an automated parser or
authenticator of a native QA/Review verdict. A trusted operator must map the
actual component verdict faithfully and include accounting uncertainty in
`obligations`; local hashes cannot prove origin or execution. This bootstrap gate
does not independently establish that CodeSensei, ReviewSensei or QASensei ran. A real
pilot claim requires their actual native receipts and qualified adapters or
explicit operator verification, retained separately from fixture conformance.

`sensei.candidate/1.0` contains `repository`, `base_commit`, `commit`, `tree`,
`diff_sha256`, `dirty`, `untracked_sha256`, `fixture_sha256` and
`environment_sha256`. The last field identifies the candidate's dependency/tool
manifest; each component may additionally retain its own execution environment.
Code's retained `candidate_manifest` artifact must equal this candidate object.
Post-Code Review and QA bind the entire same object; changed dirty/untracked
content or environment fingerprints refuse acceptance even when commit is equal.
The invoking component owns computing and verifying those source fingerprints;
Manager does not inspect a live mutable checkout. Freeze the source before use.

Artifact records contain `kind`, local `path`, `sha256`, `bytes`, `media_type`,
`producer` and `created_at`. Paths are explicit local transport details only.
Manager reads only operator-supplied paths, never fetches model-returned URLs,
executes artifact content or uses it as authority. Bytes are verified and copied
into the journal's content-addressed blob table. Original paths may disappear
after import. `export-artifact --db DB --sha256 HASH --output NEW_FILE` retrieves
the verified retained bytes without overwriting an existing file. SHA-256 covers
exact bytes, not parsed JSON. Native artifacts stay byte-for-byte unchanged.

`status`, `resume`, and `report` return the same coherent read snapshot, including
criteria/evidence per task, exact candidate, unresolved operations and next
actions. Usage remains explicitly `not_aggregated`/null; no zero-cost inference
claim is made. Publication, release and live recovery are separate, unmanaged
statuses. `events --after-seq N` exports committed sequenced events.

## Persistence, bounds and trust

One SQLite transaction commits the task, operation receipt, reserved handoff,
event and imported artifacts. SQLite serializes writers; every mutation has an
expected revision. An identical operation/payload returns its stored result even
after restart or removal of original artifact files. Reusing its ID with a
changed payload refuses. Events have append-only database triggers. A crash or
lost CLI response is reconciled by replaying that same local operation ID; this
does **not** guarantee exactly-once external effects. Original provider/component
state is untouched and remains authoritative.

Limits: 1 MiB per request and artifact, 16 artifacts per receipt, 32 tasks per
workflow. JSON rejects duplicate keys and nonfinite numbers. Canonical digests
use sorted object keys, compact separators, UTF-8, preserved array order and
unescaped Unicode. Bounded manifests reference retained artifacts. Structural
JSON Schemas are in `schemas/contracts.schema.json`; the runtime additionally
enforces graph, digest, scope, candidate and evidence invariants. The schema is
local bootstrap `sensei.manager.*`; it does not claim complete implementation of
the proposed cross-component `sensei.task/1.0` or `sensei.result/1.0` protocol.

The database and input files are operator-trusted local state. This is not a
multi-tenant service, distributed lease system, hostile-local-user sandbox or
credential store. Keep it private and exclude secrets/unrelated captures. No
credentials are read, copied or changed by Manager. Database schema versioning,
export/import migration, artifact retention policy and authenticated component
receipt adapters belong to the next reviewed increment.

## Validation and integration

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

The deterministic tests exercise a five-task fixture journey in fresh
processes, gate behavior, unknown-effect cancellation/reconciliation, duplicate
operation replay, removed original artifacts, concurrent revision fencing,
corrupt evidence/dependencies, legacy outcome compatibility, changed dirty and
environment identities, malformed JSON, cycles and overlapping writers. They
prove bootstrap behavior, not hosted G34 reachability or live provider routing.

## Development status

This standalone package is an early bootstrap, not a release or hosted-service
qualification. See [PROGRESS.md](PROGRESS.md) for completed capabilities and
[ROADMAP.md](ROADMAP.md) for the next tracked increments. The included G34 input is
a synthetic, intentionally unresolved example. It contains no account settings,
provider profile or authorization.

The next integration increment is fixed native CLI adapters with exact command
and receipt contracts, one real result round trip, and a common candidate manifest
with QA. Existing workflow databases and native operations remain authoritative.
No existing private evidence or workflow state is distributed with this source.

## License

MIT. See [LICENSE](LICENSE).
