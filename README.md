# doctask — a grounded vendor obligations register

Turns a growing stream of vendor contracts, amendments, SOWs, purchase orders, policies and
invoices into an **obligations register where every value links to exact source evidence** —
and refuses to write anything a human has not approved.

---

## Run it

You need Python 3.11+ and nothing else. **No model API key, no GPU, no model server.**

```bash
git clone <this repo> && cd doctask
make quickstart
```

That creates a virtualenv, installs the project, runs the full offline test suite, and then
drives **seven documents in four formats through the entire pipeline** — both human gates
included — printing what every stage decided and why. It takes a couple of minutes.

The demo's reviewer decisions are made by the demo reviewer credential rather than skipped,
so the gates are visibly exercised: you will see items approved, items rejected, a blocker
considered, and a classification escalated. Every claim it prints is asserted against stored
state, so the script exits non-zero the day the pipeline stops producing one of them.

### Then: crash recovery, demonstrated

```bash
make demo-crash        # needs Docker; it brings up Postgres itself
```

Starts a run, **SIGKILLs the process mid-flight three times** — once with the extracted facts
durable but uncheckpointed, once with the reviewer's decisions durable but uncheckpointed,
once with the register written but uncheckpointed — restarts the whole service stack each
time, and finishes the run. Every claim at the end is read back out of Postgres: the stage
ledger, the register, the review rows. The processes that wrote that evidence were killed
and never got to narrate anything.

---

## What the demo shows you

| # | Document | Format | What it proves |
|---|---|---|---|
| 1 | Master services agreement | PDF | Baseline register: payment 30, liability USD 250,000, notice 60; all rules pass |
| 2 | Amendment No. 1 | PDF | **Supersession surfaced, never auto-applied**; payment 30→45, notice 60→90; the liability row stays byte-identical |
| 3 | Invoice | PDF | **Source-rule violation** (NET 10 breaks PAY-01) and **item-level rejection** — the reviewer rejects the invoice's payment term, so the contractual 45 days stands |
| 4 | Data processing addendum | DOCX | Mixed-format ingestion; a provable no-op update |
| 5 | Operational notice | TXT | **Low-confidence classification escalates** to a human instead of guessing |
| 6 | Vendor portal policy | TXT | **Injection contained**: a paragraph instructing the reader to approve the document is withheld from the model, the other five blocks are processed normally, and the 5-day payment term buried in it is never extracted at all |
| 7 | Statement of work | TXT | **Unsupported claim abstained on**: the extractor asserts an anchor the text does not state, grounding refuses it, one repair is attempted, then the claim is abstained on rather than committed |

Each run prints the pipeline's own event log — stage, decision, reason, where the path
branched — plus the register diff (which rows moved, which are byte-identical) and the cost
and latency breakdown. The transcript is meant to be readable on its own.

---

## The idea in one page

**The model proposes; a human decides.** Model calls and graph nodes generate candidate
facts, conflicts, supersession proposals and rule findings. Nothing reaches the durable
register until an authenticated reviewer approves it, item by item.

**Nothing is asserted without evidence.** Every fact maps to a verbatim quote at exact
character offsets in a specific block of a specific document. A quote that is real is still
not enough: the value is re-read out of the quote, so a nearby section number, a denied
term, or a cap quoted without the exception that guts it are all refused.

**Two gates, in this order.** Source rules judge the uploaded document, before the human
sees anything. Deliverable rules judge the register *as it will stand if this run commits* —
so they run after the first gate, against the proposals the human actually approved.

**"No findings" has to be earned.** Zero violations proves nothing on its own — a stage that
never ran and a clean corpus look identical. So the report carries a denominator, and
`clean: true` additionally requires a named human who confirmed it.

**Recovery is recorded, not hoped for.** Every writing stage leaves a ledger row keyed on
`(run_id, stage, input_hash)` with the hash of what it produced, so after a crash "did this
stage complete, and with what result" has an answer.

[ARCHITECTURE.md](ARCHITECTURE.md) is the full reference: the graph stage by stage, the data
model, the concurrency and recovery design, the security invariants, and the design
decisions behind them.

---

## Task 1 requirement traceability

| # | Requirement | Implemented mechanism | Primary source files | Concrete test / demo |
|---|---|---|---|---|
| 1 | **Visible multi-stage execution** | 27-node LangGraph state machine (21 primary stages + 6 branch/recovery nodes) with dynamic routing (duplicate short-circuit, classification escalation, citation repair, abstention, no-op skip, human gates, optimistic re-derivation) | [`graph/builder.py`](src/doctask/graph/builder.py), [`graph/nodes.py`](src/doctask/graph/nodes.py), [`runtime.py`](src/doctask/runtime.py) | `tests/test_register_flow.py`, `scripts/run_demo.py` (Docs 1, 5, 7) |
| 2 | **Crash survival & resumption** | `AsyncPostgresSaver` checkpoints + idempotent stage ledger `(run_id, stage, input_hash)` to resume killed runs without state loss or duplicate writes | [`repositories/postgres.py`](src/doctask/repositories/postgres.py), [`runtime.py`](src/doctask/runtime.py), [`graph/nodes.py`](src/doctask/graph/nodes.py) | `tests/test_crash_resume.py`, `make demo-crash` (`scripts/crash_demo.py`) |
| 3 | **Item-level human gate** | Two-phase interrupts (`await_review`, `await_finding_review`) with itemized approve/reject/override decisions and cryptographic proposal binding | [`graph/nodes.py`](src/doctask/graph/nodes.py), [`domain.py`](src/doctask/domain.py), [`auth.py`](src/doctask/auth.py) | `tests/test_review_authority.py`, `tests/test_rules.py`, `scripts/run_demo.py` (Doc 3) |
| 4 | **Machine-drivable** | Full MCP server (17 tools) and FastAPI REST endpoints exposing complete pipeline lifecycle and programmatic review decisions | [`mcp_server.py`](src/doctask/mcp_server.py), [`api.py`](src/doctask/api.py) | `tests/test_mcp_integration.py`, `scripts/run_demo.py` |
| 5 | **No bluffing (grounding & abstention)** | Deterministic quote-offset verification, value re-reading from source text, 1-attempt repair loop, and explicit abstention on ungrounded claims | [`services/grounding.py`](src/doctask/services/grounding.py), [`services/citations.py`](src/doctask/services/citations.py), [`graph/nodes.py`](src/doctask/graph/nodes.py) | `tests/test_evidence.py`, `tests/test_citations.py`, `scripts/run_demo.py` (Doc 7) |
| 6 | **Stranger can run it** | 1-command reproducible setup (`make quickstart`), zero external API keys or GPU required, automated venv creation, and sensible defaults | [`Makefile`](Makefile), [`scripts/quickstart.sh`](scripts/quickstart.sh), [`.env.example`](.env.example) | `make quickstart`, `tests/test_config.py` |
| 7 | **Proves itself offline** | Deterministic offline mock LLM engine (`FakeLLM`) verifying race conditions, injections, and recovery without live model keys or cost | [`llm/fake.py`](src/doctask/llm/fake.py), [`tests/robustness_corpus.py`](tests/robustness_corpus.py) | `pytest` (25 test modules; 22 run fully offline, 3 skip without `DOCTASK_TEST_DATABASE_URL`), `tests/test_offline_resilience.py` |
| 8 | **Prompt injection defense** | Pre-extraction adversarial scanning quarantines prompt injection blocks so instructions in documents are never executed as system commands | [`services/injection.py`](src/doctask/services/injection.py), [`graph/nodes.py`](src/doctask/graph/nodes.py) | `tests/test_injection_containment.py`, `scripts/run_demo.py` (Doc 6) |
| 9 | **Concurrency & state isolation** | PostgreSQL collection-scoped advisory locks, CAS run leases, and optimistic version checks preventing cross-run state corruption | [`repositories/postgres.py`](src/doctask/repositories/postgres.py), [`runtime.py`](src/doctask/runtime.py), [`graph/nodes.py`](src/doctask/graph/nodes.py) | `tests/test_concurrent_runs.py` |
| 10 | **Cost & latency accounting** | Per-stage token metering, model pricing calculations, and wall-clock latency tracking, durably recorded in `run_events` and aggregated into a per-run cost/latency report | [`services/cost_report.py`](src/doctask/services/cost_report.py), [`services/pricing.py`](src/doctask/services/pricing.py), [`graph/nodes.py`](src/doctask/graph/nodes.py) | `tests/test_cost_report.py`, `scripts/run_demo.py` (cost/latency section), `GET /runs/{run_id}/cost` |

---

## Running the pieces yourself

The one command above is a wrapper. Everything under it is available separately.

### Install and test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                     # fully offline: no database, no model, no network
```

### Configure

```bash
cp .env.example .env
```

Every variable the application reads is in that file with a working default. Only two need
generating — the credentials:

```bash
printf 'DOCTASK_REVIEWER_TOKENS=%s:alice\n'      "$(openssl rand -hex 24)" >> .env
printf 'DOCTASK_SERVICE_TOKENS=%s:ingest-bot\n'  "$(openssl rand -hex 24)" >> .env
```

`make quickstart` generates these for you if `.env` does not exist. `.env` is gitignored;
real secrets are never committed. `tests/test_config.py` fails if `.env.example` and
`src/doctask/config.py` ever drift apart.

**Roles.** Every API and MCP call presents `Authorization: Bearer <token>`. A **service**
credential can create collections, upload documents, start runs and read everything a run
produced — the whole proposing half. Only a **reviewer** credential can approve, reject or
override a blocker, and the decision is recorded against the identity behind the token: the
request body has no actor field to put a name in. Unset means nobody authenticates, so a
deployment that forgets this rejects everything rather than accepting anyone.

### Serve the API

```bash
make api                   # http://localhost:8000/docs
```

In-memory by default. For the durable path:

```bash
make db && make migrate
export DOCTASK_REPOSITORY=postgres
make api
```

`postgres` uses `PostgresRepository` for domain state and LangGraph's `AsyncPostgresSaver`
for checkpoints, which is what makes a killed process resumable on the same `run_id`.

### Drive it by hand

```bash
COLLECTION=$(curl -sX POST localhost:8000/api/collections \
  -H "Authorization: Bearer $SERVICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Acme Vendor"}' | python -c 'import sys,json;print(json.load(sys.stdin)["collection_id"])')

curl -X PUT localhost:8000/api/collections/$COLLECTION/ruleset \
  -H "Authorization: Bearer $SERVICE_TOKEN" -H 'Content-Type: application/json' \
  --data @data/demo_pack/rules.json

curl -X POST localhost:8000/api/runs/upload \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -F collection_id=$COLLECTION \
  -F idempotency_key=msa-2026-014 \
  -F file=@data/demo_pack/01_Master_Services_Agreement_MSA-2026-014.pdf
```

The run stops at its first human gate. Find where it is, see what it has already done, and
answer it:

```bash
curl -s localhost:8000/api/runs/$RUN/status  -H "Authorization: Bearer $TOKEN"   # current stage
curl -s localhost:8000/api/runs/$RUN/stages  -H "Authorization: Bearer $TOKEN"   # ordered ledger
curl -s localhost:8000/api/runs/$RUN/events  -H "Authorization: Bearer $TOKEN"   # decision log
curl -s localhost:8000/api/runs/$RUN/review-items -H "Authorization: Bearer $TOKEN"

curl -X POST localhost:8000/api/runs/$RUN/resume \
  -H "Authorization: Bearer $REVIEWER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"idempotency_key": "review-1", "decisions": {"<item-id>": "approved"}}'
```

`/status` answers *where is it now*; `/stages` answers *what has it already done, exactly
once*. `GET /api/runs/$RUN/cost` reports what it spent and where the time went.

### Watch a folder

A collection can name a directory; anything dropped into it is ingested through the same
graph as an upload, with no manual call.

```bash
curl -X PUT localhost:8000/api/collections/$COLLECTION/watch-path \
  -H "Authorization: Bearer $SERVICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"watch_path": "/data/watch/acme"}'

export DOCTASK_WATCHER_TOKEN="$SERVICE_TOKEN"   # must be a service token, never a reviewer one
mkdir -p /data/watch/acme && make watch
```

A file is only ingested once its size and mtime are unchanged across two consecutive polls,
so a file still being copied is left alone. The idempotency key derives from the file's
SHA-256, so a restart, a re-drop of the same bytes, or a second watcher replica on the same
directory all resolve to one run. Under `docker compose up`, `watcher` runs as its own
service alongside `api` and `postgres`.

### Everything containerised

```bash
make up        # postgres + migrations + api + watcher
make down
```

### Use a real model server

```bash
export DOCTASK_LLM=gateway
export DOCTASK_LLM_BASE_URL=https://your-openai-compatible-server
export DOCTASK_LLM_API_KEY=...             # never committed
export DOCTASK_LLM_MODEL=<id from GET /v1/models>
export DOCTASK_VLM_MODEL=<vision model>    # OCR fallback only
python scripts/check_gateway.py            # verifies the server end to end
```

`DOCTASK_LLM=fake` (the default) is a deterministic offline model with real token
accounting, which is what keeps every test and both demos reproducible.

Swapping in a real model does not weaken any guarantee. The model proposes a `quote`; the
offsets are recomputed here from the block text, so an invented quote fails validation
instead of entering the register. A quote that *is* real is still re-read to check it says
what the value claims. Add the model to `config/model_prices.json` so the cost report prices
it — a model that is not in the table is reported as explicitly **unpriced** rather than
silently free.

---

## Playbooks are configuration

Rules are uploaded, not coded:

```json
{
  "code": "PAY-01",
  "severity": "major",
  "scope": "both",
  "keys": ["payment_due_days"],
  "text": "Payment terms must be at least 30 calendar days after receipt."
}
```

`scope` picks the stage — `source` runs against the uploaded document, `deliverable` against
the candidate register, `both` against each. `keys` aims a rule at named register keys in
every agreement that holds them, since the playbook names obligations, not agreements. Omit
`keys` and the rule is about the register as a whole. `severity: blocker` stops a commit;
`major`/`minor`/`info` do not.

Raising a threshold in the JSON changes the verdicts with no code change, which
`tests/test_rules.py` proves.

---

## Repository map

```text
scripts/quickstart.sh          The one command
scripts/run_demo.py            Seven documents, four formats, offline, asserted
scripts/crash_demo.py          SIGKILL a run, restart, prove exactly-once
scripts/crash_worker.py        The process that gets killed (shared with the test suite)
scripts/check_gateway.py       End-to-end check against a real model server
scripts/apply_migrations.py    Schema migrations

src/doctask/graph/             Graph state, nodes, builder
src/doctask/repositories/      Repository contract, in-memory and Postgres implementations
src/doctask/services/          Grounding, citations, injection, derivation, rules, pricing
src/doctask/llm/               Model protocol, deterministic fake, production gateway
src/doctask/api.py             FastAPI surface
src/doctask/mcp_server.py      MCP tools
src/doctask/watcher.py         Collection watch-folder worker

config/model_prices.json       Declared, versioned $/million-token price table
migrations/                    Durable data model
data/demo_pack/                The seven-document synthetic corpus the demo runs on
data/sample_data/              Minimal plain-text fixtures for the unit tests
web/                           Doctask review console (TanStack Start, evidence-backed register UI)

ARCHITECTURE.md                Full architecture reference
```

---

## Troubleshooting

**`No module named pip`** — the virtualenv was created by `uv`, which does not install pip.
`python -m ensurepip --upgrade`, or let `make quickstart` handle it.

**pip cannot find a wheel on a very new macOS** — an older pip fails to parse the OS version
and selects no wheel. `python -m pip install --upgrade pip` fixes it; `make quickstart` does
this automatically.

**`make demo-crash` says it cannot reach Postgres** — it starts Postgres itself via Docker,
so Docker must be running. `make demo` needs none of this.

**Postgres integration tests are skipped** — that is deliberate: `pytest` alone stays fully
offline. `make test-pg` brings up the database and runs them.
