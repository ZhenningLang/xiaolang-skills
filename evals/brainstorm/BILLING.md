# Billing, recovery and the model backend

Detail for the scoring pipeline described in [evals/README.md](../README.md). The short version of how money is controlled is there; this page is the full contract of the ledger and of the Claude backend.

## Offline Backend Example

```python
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger, guarded_call

ledger = BudgetLedger("evals/brainstorm/runs/example/ledger.fake.jsonl", limit="50")
result = guarded_call(ledger, FakeBackend(), "example_attempt_1", "Say hello", "test", 20)
print(result.text)
```

Always route model calls through `guarded_call`. A call key identifies the exact
request (exam, configuration, repetition, agent, batch, prompt hash, model,
attempt). Reuse a key only for the same request; retry a paid failure with a new
attempt. The caller owns that identity contract, not this billing layer.

`Backend` supplies `name`, `estimate_max_usd(prompt, model, max_output_tokens)`,
and `call(prompt, model, max_output_tokens)`. `CallResult` contains `text`, `usd`,
`tokens` (`input`, `cache_write`, `cache_read`, `output`), `raw_ref`, and `outcome`.
Use `Decimal` or decimal strings for configured amounts. FakeBackend accepts
`usd`, `reserve_usd`, and `failure` (`not_started`, `paid_error`, `unknown`, or
`missing_cost`); setting `usd > reserve_usd` simulates an overrun.

## Billing And Recovery

The default limit is USD 50. Reservations are checked and persisted under a
stable file lock before starting a backend. Used budget is settled cost plus
pending reservations and uncertain calls, counted once per key. A reservation
equal to the remaining budget is allowed; a larger one never starts the backend.
Money is written as JSON numbers and accounted for with decimal arithmetic.
Call reservations must be positive; configure a conservative model price rather
than treating unknown charges as free. Amounts outside the supported Decimal
exponent range are rejected before changing the ledger.

Success writes an atomic cache before settlement. Cache hits cost nothing.
An error with known cost is settled as `paid_error`, without a success cache.
If result validation or cache persistence fails while the ledger remains writable,
a known charge is settled as `paid_error`; an unknown charge remains uncertain.
If settlement cannot be persisted, the process permanently disables all guarded
calls, including calls through new ledger objects or a different ledger path.
The error includes the call key, reserved amount, actual amount, and delta.
An actual overrun uses exit code 3 even when persistence fails; other persistence
failures use exit code 1. Callers must exit, not catch the error and continue.

On a transaction write failure, while still holding the settlement lock, the process tries to fsync billing
evidence into that existing lock file. If this succeeds, other processes also
stop before admitting new reservations. No additional persistence layer is used.
If both ledger and lock writes fail, evidence is available only in the error;
cross-process or post-restart stopping cannot be guaranteed. Do not treat the
old reservation as the actual charge after storage recovers. Preserve the error,
ledger, cache, and lock; stop all clients and reconcile the missing charge before
any restart. There is no automatic unlock/recovery command: a halted ledger's
status, settle, and release commands currently refuse access. Recovery policy
requires an operator decision, not erasing evidence or starting an empty ledger.
Automatic settlement handles its own call only; it does not recover unrelated
dead owners. Reserve, status, and manual reconciliation retain that recovery.
Failures to acquire or read the ledger before the billing transaction can stop
only the current process, without persistent lock evidence. Preserve their error
diagnostics as well; this is not a cross-process guarantee after storage recovery.
A successful response without cost settles its full reservation as `estimated`;
status reports this separately. An unreadable result keeps its reservation as
`uncertain`, not as zero. Only a definitely unstarted request is automatically
released. The adapter never retries model requests.

A dead owner's cached result repairs its missing settlement. A dead owner
without a cache becomes uncertain and cannot be resent. Corrupt caches or ledger
records fail closed. Process ownership uses POSIX PID liveness; do not copy an
active ledger between machines or manually edit it. Empty existing ledgers and
missing ledgers with surviving caches are corruption, not fresh budgets. Ledger events are logically
append-only: atomic replacement preserves the complete previous prefix. Locks
and caches are adjacent to the resolved ledger path, so multiple run directories
using one ledger also share recovery and exclusion.

If actual cost exceeds its reservation, the actual amount is still recorded,
then **exit code 3** is used at the CLI boundary. The diagnostic includes
reserved, actual, and delta. All new calls on that ledger stop, even if total
spending is below its limit; restarting or increasing the limit does not clear
this condition. Existing successful caches can still be read. This protects
subsequent calls, but a **single call** can exceed its reservation and budget.

`default_ledger_path(backend, run_dir)` selects `run_dir/ledger.fake.jsonl` for
fake calls and the state-directory path (see "Where Data Lives" in the evaluation README) for all real backends,
independent of run directory. An explicit ledger path overrides this selection;
do not use a new empty real ledger to evade cumulative costs. Private exams and
run outputs are ignored by Git. Do not publish raw model responses or caches.

```sh
python3 -m evals.brainstorm --help
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl status
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl settle CALL_KEY --usd 0.01
python3 -m evals.brainstorm ledger --ledger path/to/ledger.jsonl release CALL_KEY --confirm-not-charged
```

Status prints JSON: `used`, `settled`, `reserved`, `uncertain`, `estimated`,
`remaining`, `limit`, and `overruns`. Manual reconciliation refuses live owners.
Repeating an identical settlement or release is idempotent. Settlement without a
cached response reconciles billing only; it does not fabricate a usable result.
Release requires independent evidence that no charge occurred. Settled calls
cannot be released or given a different cost. Other errors use exit code 1.
Zero-charge manual reconciliation must use the confirmed release command, not
`settle --usd 0`. Limits remain caller-configured; pass the same limit to every
client and status command sharing a ledger.

## Claude Boundary

`ClaudeCliBackend(pricing=...)` invokes `claude -p` with the prompt on stdin,
empty setting sources and tools, strict MCP configuration, disabled commands,
JSON output, and no session persistence. It parses cost even on a nonzero exit.
Only a failure to construct the subprocess counts as unstarted; communication
errors after startup retain the reservation. Missing usage fields are not
fabricated as zero tokens: the result is an unusable paid error, with its known
charge preserved. It never uses shell interpolation. Tests do not invoke a real model.

Pricing is required for each model; there is no unknown-model fallback:

```python
pricing = {"your_model": {"fixed_tokens": 10000,
                          "input_per_million": "CONFIGURE_CURRENT_RATE",
                          "output_per_million": "CONFIGURE_CURRENT_RATE"}}
```

These are configuration placeholders, not quoted prices. The estimate is
`((fixed_tokens + UTF-8 prompt bytes) * input_per_million
+ max_output_tokens * output_per_million) / 1000000`. Configure fixed overhead
and conservative rates to cover hidden context and cache-write pricing.

**Output cap boundary:** the subprocess environment explicitly sets
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `max_output_tokens`, overriding any inherited
value without changing the parent environment. CLI version 2.1.316 contains the
environment-variable reader and corresponding output-limit diagnostics. The
official reference is <https://code.claude.com/docs/en/env-vars>; the docs page
could not be fetched during this implementation round. This setting is not a
proven universal hard cap, nor is the CLI budget option a reliable pre-call hard
limit. The adapter does not retry requests; the vendor CLI's internal retry
behavior is outside that guarantee. Personal configuration or automatically loaded
context may affect the prompt despite the isolation flags; its absence remains
unverified. Real cost fidelity and personal-context contamination require a
separate explicitly authorized, budgeted smoke test. No real smoke is part of
the automated suite, and a reservation is not a vendor-enforced spending cap.
