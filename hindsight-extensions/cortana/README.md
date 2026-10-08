# hindsight-ext-cortana

Cortana's extension package for this fork of Hindsight. The fork's branch `cortana` is cut
from upstream tag `v0.10.2`; our behavior lives here, loaded through the engine's extension
slots, and the engine's own code stays as upstream released it. The binding specification is
`docs/work/hindsight/specs/HINDSIGHT.md` in Cortana's vault.

Unlike upstream's extension folders, this one is an installable distribution with its own
`pyproject.toml`. It depends only on libraries the engine already depends on, and not on the
engine itself, which is the host process.

## Slots

The server loads one class per slot from its environment:

```bash
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_ext_cortana:CortanaOperationHooks
HINDSIGHT_API_HTTP_EXTENSION=hindsight_ext_cortana:CortanaHttpExtension
HINDSIGHT_API_MCP_EXTENSION=hindsight_ext_cortana:CortanaMcpExtension
HINDSIGHT_API_TENANT_EXTENSION=hindsight_ext_cortana:CortanaTenantExtension
```

Set all four together. The tenant extension owns the migrations and the bank-scoped table
declarations; the separate worker (`hindsight-worker`) needs the same variables as the API.

| Module | What it holds |
| --- | --- |
| `hooks` | `CortanaOperationHooks`: `validate_retain`'s pool scoping (HSIGHT-6); `on_retain_complete`, where structuring and supersession run (HSIGHT-4, HSIGHT-5); the retrieval log hooks (HSIGHT-7); `filter_mcp_tools`, narrowing `/mcp` to our three tools (HSIGHT-6) |
| `http` | `CortanaHttpExtension`: routes under `/ext/cortana/`: `status`, `retrievals`, `ledger` (HSIGHT-7); `current`, `subjects`, `facts/<id>`, `decisions` (HSIGHT-6) |
| `mcp` | `CortanaMcpExtension`: `cortana_current`, `cortana_subjects` and `cortana_record_decision` on the engine's `/mcp` (HSIGHT-6) |
| `current` | the current-state read behind `current`, `subjects` and `facts/<id>` and their tools (HSIGHT-6) |
| `decisions` | decision capture behind `POST /ext/cortana/decisions` and `cortana_record_decision` (HSIGHT-6) |
| `pools` | pool scoping for documents that arrive without a pool (HSIGHT-6, closing COR-18) |
| `tenant` | `CortanaTenantExtension`: the engine's default tenant plus our migrations and bank-scoped tables |
| `migrations` | the Alembic branch `cortana` and the table names |
| `extraction` | the versioned extraction instructions (`instructions.md`, `VERSION`) and the bank configuration that applies them |
| `structuring` | the versioned structuring prompt (`prompt.md`, `VERSION`), batching, validation, the runner, the engine adapters `on_retain_complete` uses (`structuring.engine`), the structuring suite (`structuring.suite`) and the fixtures' schema and loader (`structuring.fixtures`) |
| `rules` | the supersession rules S1 to S11 as a pure function of a key's valid claims (HSIGHT-5) |
| `supersession` | applying the rules: claim and key states, the ledger, and retirement, restoration and reason updates through the engine's `update_memory_unit`, with mental-model refresh requests |
| `alignment` | the versioned alignment prompt (`prompt.md`, `VERSION`) and the pass that resolves keys pending alignment by merge or as distinct |
| `merges` | attribute merges, their inverse, and marking a key distinct |
| `ledger` | the append-only ledger's writer, its event names, and the query behind `GET /ext/cortana/ledger` |
| `retrievals` | the retrieval log: rows built from the recall and reflect hooks, the write, the retention sweep and the query behind `GET /ext/cortana/retrievals` |
| `status` | `build_status`, behind `GET /ext/cortana/status` and `hindsight-cortana status` |
| `reconcile` | `after_retain` (the hook's work after structuring) and `reconcile` for a subject, a document or the bank, with the orphan sweep |
| `gate` | the evaluation gate (HSIGHT-8): the runner (`runner`), the strict scorer (`strict`), the questions generated from the ledger (`questions`), the reflect judge (`judge`) and the acceptance run (`acceptance`); and the latency measurement and its budget (`latency`, `latency_budget.json`, `synthetic`; HSIGHT-9) |
| `migrate` | the migration's resumable structuring pass behind `hindsight-cortana migrate structure` (specification 13.4 step 4; HSIGHT-8) |
| `cli` | `hindsight-cortana` |
| `verify_base` | the base check behind `hindsight-cortana verify-base` |

## Extraction instructions

Every new fact states one claim, names its subject, gives its value in full, words a state in the
moment as provisional and a resolution as a resolution (specification 5.1). The engine carries this
through its `custom` extraction mode: `retain_custom_instructions` replace its concise guidelines,
and the rest of its prompt and its output schema stay as they are. The instructions name no person;
the bank's retain missions say whose memory it is.

Apply them with the engine's configuration API, `PATCH /v1/default/banks/<bank>/config` with
`{"updates": hindsight_ext_cortana.extraction.bank_config_updates(<the bank's retain_strategies>)}`.
A retain strategy that sets its own extraction mode overrides the bank's, so the updates switch
every strategy that extracts with a model (`concise`, `verbose`) to `custom` and leave `chunks` and
`verbatim` strategies alone. The production bank changes only at cut-over (HSIGHT-10).

A change to `extraction/instructions.md` is a release: raise `VERSION`, re-pin the hash in
`tests/test_extraction.py`, re-cut the structuring fixtures, and run the gate.

## Structuring fixtures

Fixture chunks, the facts the engine's dry-run extraction produced from them under the
instructions, and the claims structuring must end with, in the form `structuring.fixtures` defines.
This repository is public, so it carries only synthetic fixtures with invented names and facts
(`tests/fixtures/structuring/`). Fixtures cut from real sessions and vault documents stay on the
operator's machine, in `~/.cortana-legacy/hindsight/fixtures/structuring/` or the folder named by
`HINDSIGHT_CORTANA_REAL_FIXTURES`; the suite checks them when the folder exists and skips, saying
why, when it does not.

## Structuring

After every retain, `on_retain_complete` turns the new facts into claims (specification 5.2):

1. It reads the facts, their chunks and their entities (label entities left out), and skips facts
   that already have claims.
2. It packs them into calls, at most `MAX_FACTS_PER_CALL` facts and `MAX_CHUNKS_PER_CALL` chunks
   each, whole chunks where possible (`structuring/batching.py` gives the reasons).
3. For each call it shows the model the source (a session's numbered, timestamped turns or a
   document's text), the attribute catalog of every subject the facts name, and the facts, and asks
   for each fact's claims (`structuring/prompt.md`).
4. The call goes through the engine's retain provider bound to the bank, with two retries.
5. Code validates the answer (`structuring/validation.py`): subjects resolve to the fact's entities
   or to existing entities (the engine's resolver, read-only); keys are normalized; a new key on a
   subject that already has keys is `unaligned` unless `same_as` maps it; an earlier-state marker
   makes a claim provisional; the statement-time tuple and the content hash are computed.
6. It writes `claims`, new `attributes` and one `structured` ledger entry per call. A failed call,
   or a fact the answer gave no valid claim, is a `structuring-pending` ledger entry for
   reconciliation, which structures such facts with `structuring.engine.structure_facts`.

Claims that arrive already structured, such as decision records (HSIGHT-6), go through
`structuring.engine.record_prestructured`: the same validation, no model call, the decision's
moment and rank.

Statement time (`claims.stated_at`, with `stated_at_source` saying where it came from): a session
claim takes the timestamp of the turn the model names, else the first timestamp of its chunk, else
the session's start; a document claim takes the date of the dated entry it comes from, at noon US
Eastern, else the document's stamped date; a decision takes its moment. The tuple's other elements
are the item's position in the retain, the chunk index, the fact's position in its chunk (by
`mentioned_at`), and the source rank (session 0, document 1, correction 2, decision 3).

A change to `structuring/prompt.md` is a release: raise `VERSION`, re-pin the hash in
`tests/test_structuring_batching.py`, and run the structuring suite.

### The structuring suite

Real model calls on the structuring fixtures, through the engine's retain provider configured from
the `HINDSIGHT_API_*` environment, scored for precision on key and provisional flag (target 0.95),
recall, statement time and key stability across runs:

```bash
cd ~/.hindsight/daemon-cwd   # never a repository: the model's working directory
env $(grep -E '^HINDSIGHT_API_(LLM|RETAIN_LLM)_' ~/.hindsight/profiles/cortana-scratch.env | xargs) \
  uv run --project <checkout>/hindsight-extensions/cortana hindsight-cortana suite structuring \
  --fixtures ~/.cortana-legacy/hindsight/fixtures/structuring \
  --fixtures <checkout>/hindsight-extensions/cortana/tests/fixtures/structuring \
  --out <a folder outside the repository>
```

It exits 0 when every run meets the target. Its output holds claims about real data; keep it out of
this repository.

## Tables and migrations

`claims`, `attributes`, `ledger` and `retrievals` (specification section 4.2) are created by the
revisions in `hindsight_ext_cortana/migrations/versions/`. They form an Alembic tree of their own,
labelled `cortana`, whose base depends on the engine's head at v0.10.2 (`e5b1c7d3a902`). The
engine applies it at startup in its own migration run, under its advisory lock. Because our base
depends on the engine's head, `alembic_version` records our head in place of the engine's (Alembic
treats the engine's head as applied through ours); the engine reads that table only through
Alembic. No table has a foreign key to an engine table, the ledger refuses `UPDATE` through a
trigger, and the tenant extension declares all four as bank-scoped, so the engine's bank deletion
and `hindsight-admin` backup and restore include them.

A new revision goes in `versions/` with `down_revision` set to the current `cortana` head. The
engine has no `alembic.ini`; generate one with `alembic.command.revision`, configured as
`hindsight_api.migrations` configures a run (core's `alembic` folder as the script location, core's
`versions` and ours as version locations) and `head="cortana@head"`.

## Supersession and reconciliation

After structuring, `on_retain_complete` sweeps orphaned claims of the retain's documents, aligns the
pending keys among the retain's claims, and settles every key the retain touched: the rules decide
each claim's state, changed states are written with their ledger entries, and facts whose claims
are all superseded are retired through the engine's curation path with the reason
`superseded by <fact id> on <subject>/<attribute>, rule <id>` (restored with `state: valid` when a
claim becomes valid again). Mental models citing a retired fact, or an observation the retirement
deleted, are asked to refresh.

The operator's commands open the engine from the profile's environment as `hindsight-worker` does
(source the profile first; the server's worker runs what they queue):

```
hindsight-cortana reconcile --bank <id> --all          # or --subject <entity id or name>, --document <id>
hindsight-cortana merge --bank <id> --subject <s> --key <k> --into <target>
hindsight-cortana unmerge --bank <id> --subject <s> --key <k>
```

`reconcile` structures facts that have no claims and no answered structuring call, aligns pending
keys, sweeps orphaned claims, settles every key in scope and writes a `reconciled` ledger entry only
when something changed, so a second run writes nothing. A whole-bank run also deletes the bank's
retrieval log rows past retention (see Observability).

## The current-state read and decision capture

Specification sections 9 and 10.1, built by HSIGHT-6. Every route takes `bank_id`; the MCP tools use
the connection's bank (`/mcp/<bank>/`).

- `GET /ext/cortana/current?bank_id=&subject=&attribute=` (`cortana_current`): each key's current
  claim with the fact's text (Josh's words for a decision record), later provisional statements, a
  conflict's claims newest first, the documents that still say otherwise, and the superseded claims
  newest first with their rules; `summary` says it in one line per key. A subject that matches
  nothing, or a key with no claim, is "no position recorded". A query over `claims` joined with the
  engine's facts and the key's catalog row: no model, no ranking.
- `GET /ext/cortana/subjects?bank_id=&q=` (`cortana_subjects`): subjects whose name contains the
  text, or that the engine's entity resolution maps it to, with their keys, current values and last
  statement times.
- `GET /ext/cortana/facts/<fact id>?bank_id=`: one fact's claims and their states.
- `POST /ext/cortana/decisions?bank_id=` (`cortana_record_decision`): Josh's words verbatim, the
  subject, key, value, statement time (now by default), session id and domain. It retains one
  document `decision:<operation id>` under the bank's `decision` strategy (extraction mode `chunks`:
  no model call, the words are the fact), tagged `source:decision`, `domain:<d>` and the domain's
  pool; records the claim from the arguments with the decision rank; settles the key; and answers
  in one line naming the fact, the key and what it replaced. The same words, subject, key and time
  record nothing new. A bank without a `decision` strategy gets one the first time; one configured
  with another extraction mode is refused.

A later claim that is not itself a decision record, with the same value as a standing decision
record, is superseded by it as a restatement (the S3 exception in `rules`), so the session's own
save of the words, or a document edited to agree, never retires Josh's verbatim record.

`/mcp` exposes only these three tools (`filter_mcp_tools`); the plugin's stdio server keeps recall
and reflect. Documents that arrive without a `pool:` tag get the pool of their `domain:` tag, or
`general`, with that pool as their observation scope; tagged saves are left as they came.

## Observability

Specification section 12, built by HSIGHT-7.

**Retrieval log.** `on_recall_complete` writes one `retrievals` row for every recall, including the
recalls reflect makes through its tools and the engine's internal ones (consolidation's), and
`on_reflect_complete` one row for every reflect that returned an answer (the engine calls no hook
for a failed reflect; its recalls are still logged). A recall row holds the query, the caller as
the request context shows it (never the API key), the parameters, and the ids returned in rank order
with their scores; a failed recall holds the error. A reflect row holds every tool call in order
with its input, reason and returned ids, and the ids the answer cited (`cited_ids`,
`cited_mental_model_ids`). `reflect_id` links a reflect to the recalls it made. Rows are written
synchronously in the hook; a failed write is logged and never fails the call.

**Retention.** `HINDSIGHT_CORTANA_RETRIEVALS_RETENTION_DAYS`, default 30, because a wrong answer
noticed within a month must be diagnosable from the record without re-running the call. A
whole-bank `reconcile` deletes the bank's older rows; `hindsight-cortana retrievals sweep [--bank
<id>] [--days <n>]` does it on demand for one bank or all.

**Routes.** All newest first, filtered by query parameters, `limit` up to 1000:

- `GET /ext/cortana/retrievals?bank_id=&kind=recall|reflect&since=&reflect_id=&limit=`
- `GET /ext/cortana/ledger?bank_id=&subject=&attribute=&fact_id=&document=&since=&limit=`; `subject`
  is an entity id or an exact name, and subject, attribute and document must hold for one claim
- `GET /ext/cortana/status?bank_id=`: migrations and tables; facts without claims and pending
  structuring; unaligned claims, pending keys, conflicts and stale-document markers; the last
  reconciliation that changed something; `gate` (the newest summary `hindsight-cortana gate` wrote to
  the operating folder's `ranking/`, shape `status.GateRun`, or null); fact retirements and restorations in the last day; the worker (in-process or
  separate, the separate worker's liveness probe on `HINDSIGHT_API_WORKER_HTTP_PORT`, and the
  operation queue); the retrieval log's rows and any past retention; and the check that `~/.env`
  holds no `HINDSIGHT_` key. `problems` lists whatever is out of order.

`hindsight-cortana status [--bank <id>]` prints the same JSON and exits 1 when `problems` is not
empty.

**Engine logging and the audit log** are configuration: `HINDSIGHT_API_LOG_FORMAT=json` for
structured log lines, and `HINDSIGHT_API_AUDIT_LOG_ENABLED=true` with
`HINDSIGHT_API_AUDIT_LOG_ACTIONS=reflect` so the engine keeps each reflect's raw request and response
in its `audit_log` table, read through `GET /v1/default/banks/<bank>/audit-logs`.

**Diagnosing a wrong answer:** find the reflect in `/ext/cortana/retrievals?kind=reflect`, read its
tool calls and cited ids (and its recalls' scores with `reflect_id=`), look up each cited fact, and
read `/ext/cortana/ledger?fact_id=` or `?subject=&attribute=` for its key.

## Performance isolation and the latency budget

Specification section 14 and acceptance criterion 13, built by HSIGHT-9.

**The worker arrangement** is configuration plus one launchd job. The API runs with
`HINDSIGHT_API_WORKER_ENABLED=false`; the engine's `hindsight-worker`, started by
`~/.cortana-legacy/hindsight/launchd/start-worker.sh` (launchd job
`com.jitwaru2.hindsight-worker`) from the same profile, runs consolidation, mental-model refreshes
and asynchronous retains, including this package's structuring, alignment and supersession, with
its own reranker threads and recall semaphore. Live recall and reflect stay in the API. Our hooks
run in whichever process does the work: `on_retain_complete` in the worker, `on_recall_complete`
and `on_reflect_complete` in the API for live calls and in the worker for consolidation's and
refreshes' internal recalls. `HINDSIGHT_API_WORKER_HTTP_PORT` in the profile is the worker's
liveness port; the status route probes it.

**The measurement.** `hindsight-cortana latency --bank <id> [--url <server>]` times live recall
and reflect over HTTP, one call at a time after a few warm-up recalls, first with no background
work and then while a retain and a consolidation run, and checks the 50th and 95th percentiles
against `hindsight_ext_cortana/gate/latency_budget.json`. It exits 1 on any breach: a percentile
over its ceiling, recall p95 under load over its allowed multiple of idle p95, a failed call, or
load that was not running for the required share of probes. `--out` writes every sample.

- Queries come from the bank (fact texts for recall, its most-mentioned entities for reflect)
  unless `--queries` names a JSON file with `recall` and `reflect` lists.
- The load runs on a separate synthetic bank, `<bank>-load` unless `--load-bank` names another,
  so the measured bank is never changed; the contention measured is the server's (reranker
  threads, recall semaphore, database pool, CPU). The command applies the atomic extraction
  instructions and auto-consolidation to the load bank, triggers a consolidation, keeps one
  asynchronous retain of a multi-chunk synthetic document in flight, starts probing once both are
  running, records at every probe whether they still are, and cancels what is left on the load
  bank when it finishes. A load bank holding unconsolidated facts gives consolidation work from
  the first probe; an empty one waits for the first retain to finish.
- The recall probe is the engine's default recall (all fact types, `mid` budget, 4,096 tokens),
  which reranks the full candidate set; the reflect probe is the engine's default reflect.

**The budget** is versioned with the package: `latency_budget.json` holds the ceilings, the sample
counts, the load's settings, the reason for each and the measurements they were set from. A change
to it is a release (specification 11). `hindsight-cortana gate` runs the same measurement as its
latency suite.

## The migration's structuring pass

`hindsight-cortana migrate structure --bank <id>` structures every valid world and experience fact
that has no claims and that no structuring call has answered (specification 13.4 step 4), document by
document in statement-time order, each document in the structuring batches with the source hints its
retain recorded (`documents.retain_params`: event date, context). Legacy compound facts receive
several claims; nothing is re-extracted. Then `hindsight-cortana reconcile --bank <id> --all` aligns
pending keys and applies supersession.

- **Resumable.** Each call's `structured` ledger row is its progress; rerun the same command to
  continue. The pass writes `structure-pass-started` and `structure-pass-finished` or
  `structure-pass-stopped` (actor `operator`) with its counts, and holds a per-bank advisory lock, so a
  second pass on the same bank refuses to start. Stop it with SIGTERM: a batch is written in one
  transaction, so a call in flight is simply asked again next time.
- **Priority.** `--priority-subjects <file>` takes a JSON list of case-insensitive regular expressions;
  documents with a fact whose text or entity names match go first, whole. `--phase priority|rest|all`.
- **Concurrency.** `--concurrency` documents in flight (default `migrate.DEFAULT_CONCURRENCY`, set from
  the rehearsal's measurement). Answers are validated and written one at a time against the catalog as
  it stands then, so a key another document created meanwhile is joined or, under a new name, left
  pending for alignment.
- **Failures.** A failed call is retried with a doubling backoff (`--retries`, `--backoff`), then its
  facts are left `structuring-pending` for the next run. A usage limit stops the pass at once; so does a
  run of failed calls (`--max-consecutive-failures`). `--progress-file` appends a line per call with
  the facts per hour and the projected time left; `--plan-only` prints what is left.

## The gate

`hindsight-cortana gate --bank <id> --url <server>` runs the evaluation gate of specification 11 and
exits 0 only when every suite ran and passed:

1. **deterministic**: this package's tests (`pytest`, no model calls), from a checkout;
2. **structuring**: the structuring suite on the real fixtures (`HINDSIGHT_CORTANA_REAL_FIXTURES`);
3. **acceptance**: the ten questions of the operating folder's questions file, scored against its answer
   keys by the strict scorer, three ways each: the current-state read (starting from the question's
   subjects in `ranking/acceptance-subjects.json`), plain recall (first result; loose and strict), and
   reflect judged by a model with the key's patterns as rubric; then questions generated from the
   ledger, one per key superseded in the window (default: since the newest earlier gate run on the
   bank; `--since`, `--all-supersessions`), read and recalled all, reflected on a seeded sample
   (`--reflect-sample`). Hard criteria: the read and reflect; recall is measured, and its failure means
   HSIGHT-11 before cut-over (specification 16 item 8);
4. **latency**: the HSIGHT-9 measurement against `latency_budget.json`.

`--suites` selects a subset. Each run writes `gate-<time>.md` (the table, also printed) and
`gate-<time>.json` (the summary, every result included) to `$HINDSIGHT_CORTANA_GATE_DIR`, else
`~/.cortana-legacy/hindsight/ranking/`; `status.gate` reports the newest. The questions, keys, subjects
and fixtures are real data and stay in the operating folder, never in this repository
(`HINDSIGHT_CORTANA_ACCEPTANCE_QUESTIONS`, `..._KEYS`, `..._SUBJECTS` move them). The acceptance run
reads the ledger through the engine, so source the profile of the server's database first, as for
`reconcile`.

## Tests

The deterministic suite runs on the engine's own fixtures: an embedded PostgreSQL through pg0 (its
own instance, `hindsight-ext-cortana-test` on port 5557, separate from the engine's test instance),
the local embedding and reranking models, and the `mock` LLM provider. Nothing calls a model.

```bash
cd hindsight-extensions/cortana
uv sync
uv run ruff check . && uv run ruff format --check . && uv run deptry .
uv run pytest
```

`.python-version` pins 3.14, production's interpreter. `.github/workflows/cortana.yml` runs the
same steps on Python 3.11 (the development checkout's) and 3.14 for every push to `cortana`. The
structuring suite, which calls a model, is separate (see Structuring).

## A scratch server from the development checkout

The embed daemon's development path runs the API with
`uv run --project <checkout>/hindsight-api-slim --extra all hindsight-api`, which installs the
engine's project but not this package. Install it once, editable, into the checkout's environment;
the engine's `pyproject.toml` stays untouched:

```bash
uv pip install --python <checkout>/.venv/bin/python --editable <checkout>/hindsight-extensions/cortana
```

`uv run` syncs inexactly, so the install survives every daemon start. A plain `uv sync` in the
checkout is exact and removes it; repeat the install after one (HSIGHT-1's syncs used
`--inexact`), and after any pull that adds a dependency to this package (HSIGHT-4 added
`python-slugify`, which the engine does not carry). Then put the four variables above in the scratch profile and start the daemon from
the checkout as HSIGHT-1 documents.

## Production

Installed beside the engine from the release tag (specification section 13.1):

```bash
uv tool install hindsight-embed==0.10.2 \
  --with 'hindsight-api-slim[all] @ git+https://github.com/jitwaru2/hindsight@<tag>#subdirectory=hindsight-api-slim' \
  --with 'hindsight-ext-cortana @ git+https://github.com/jitwaru2/hindsight@<tag>#subdirectory=hindsight-extensions/cortana'
```

## verify-base

`hindsight-cortana verify-base <RECORD> <package dir>` proves the fork's base is the release we
run. It compares the checkout's `hindsight-api-slim/hindsight_api/` with the SHA-256 values in the
installed wheel's `RECORD`: every file the RECORD lists must exist with the same hash, and the
checkout must hold no package file the RECORD lacks (`__pycache__` is ignored).

```bash
hindsight-cortana verify-base \
  <site-packages>/hindsight_api_slim-0.10.2.dist-info/RECORD \
  hindsight-api-slim/hindsight_api
```

The same check runs without the engine installed, under any Python 3.9 or later, as
`python3 -I hindsight-extensions/cortana/hindsight_ext_cortana/verify_base.py <RECORD> <dir>`.

Use `hindsight_api_slim-<version>.dist-info/RECORD`, which lists the code. The
`hindsight_api-<version>.dist-info` beside it is a metadata-only package with no code to compare.
The command prints matching, mismatched, missing and extra counts with the paths that differ, and
exits 0 only when everything matches. Repeat it after every upstream pull, against a fresh install
of the new upstream version, before tagging a `v<upstream>-cortana.<n>` release.
