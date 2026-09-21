# Pipeline Log Triage - design notes

## What the script does today

`Triage.py` is a single-shot CLI. It reads one pipeline log file off disk,
sends the whole file to Claude in one request via the `claude` CLI, prints
the triage back to stdout, and appends a record of the result to a local
history file. There is no loop, no tool use, and no multi-turn conversation
- each invocation is one independent triage - but a thin layer of state
does now persist across separate invocations (see "Persistence" below).

Flow:

1. Validate argv - exactly one argument, the log path.
2. Read the file (tolerating a non-UTF-8 encoding, see "Log varieties"
   below); bail if unreadable or blank after `strip()`.
3. Build a single prompt: the on-call-engineer system prompt, explicit
   instructions to respond with only a JSON object matching the `Triage`
   shape, and the log wrapped in a `<log>` tag with its path.
4. Run `claude -p "<prompt>" --model claude-haiku-4-5 --output-format json`
   as a subprocess. This routes the request through the user's Claude
   subscription (`claude login`) rather than a billed Anthropic API key.
5. Parse the CLI's JSON envelope, pull out its `result` field, strip a
   markdown code fence if the model added one anyway, and validate it
   against the `Triage` pydantic model.
6. Print the result via `print_report()`, then append it to the local
   history file via `_append_history()`.

The system prompt asks Claude to break the run into **distinct failures** -
one entry per root cause. Downstream noise (tasks that only failed because an
earlier task did) gets folded into the entry for the failure that caused it,
rather than becoming its own entry.

### Why the CLI instead of the Anthropic SDK

The script originally called `anthropic.Anthropic()` directly with an API
key (`ANTHROPIC_API_KEY` env var or `ant auth login`), which uses
pay-per-token API billing and gave native structured-output support
(`messages.parse(output_format=Triage)`, guaranteed schema-valid). It was
switched to shell out to the `claude` CLI (`claude -p ...`) instead, so runs
are billed against the user's Claude subscription rather than the API. The
trade-off: the CLI has no equivalent to `output_format`, so schema
compliance is now enforced by prompt instructions only (`_strip_code_fence`
+ pydantic validation as a safety net, not a guarantee) - a malformed or
truncated response is a real failure mode now, surfaced as a clean
`ClaudeCliError` rather than a crash.

## Input format

A plain-text log file, passed as the only positional argument:

```
python triage.py sample_pipeline.log
```

No format is assumed beyond "text a pipeline emitted." `sample_pipeline.log`
is the reference single-root-cause shape: `TIMESTAMP LEVEL COMPONENT
MESSAGE`, one event per line. `sample_multi_failure.log` is the
multi-root-cause fixture (see below). The file is sent verbatim, wrapped in
a `<log>` tag with its path, and is never truncated or chunked.

## Output format

**Structured shape requested from the model** (`Triage`, enforced only by
prompt instructions plus a pydantic validation safety net - see above, this
is not a hard guarantee the way the old SDK's `output_format` was):

| Field | Type | Meaning |
| --- | --- | --- |
| `failures` | list of `Failure` | one entry per distinct root cause, most-likely-blocker first |
| `failures[].failure_type` | str | short label, e.g. `"upstream schema drift"` |
| `failures[].what_broke` | str | the root failure in plain English |
| `failures[].evidence` | str | the log lines/values that point to it |
| `failures[].next_step` | str | next action to fix or confirm |
| `notes` | str | cross-failure context or a named ambiguity; `""` when there is nothing to add |

`print_report()` renders this as a formatted report: a `"N distinct failures
found."` header when there's more than one, a numbered section per failure,
and a trailing `Notes:` line when `notes` is non-empty.

Exit codes: `0` on success; `1` for a usage error, an unreadable or empty log
file, a missing/timed-out/failing `claude` CLI, or a response that doesn't
parse as valid JSON or doesn't match the `Triage` schema (each with a
one-line message on stderr).

## Backends: local CLI vs. hosted API

`run_triage()` picks the transport; the prompt, JSON schema, and validation
are shared (`_log_message`, `_parse_triage_text`), so the two paths differ
only in how the request is sent:

- **`cli` (default, local):** `claude -p ...` subprocess, billed to the
  user's Claude subscription.
- **`api` (Vercel, or `TRIAGE_BACKEND=api`):** `anthropic` SDK
  `messages.create` with a billed `ANTHROPIC_API_KEY`. Needed because a
  serverless function can't hold an interactive `claude login` session.
  `anthropic` is imported lazily, so the local CLI never needs it. SDK
  failures map to `ClaudeApiError` (a `ClaudeCliError` subclass) with clean
  one-line messages; a `max_tokens` stop is reported as truncation rather
  than surfacing as a confusing JSON parse error.

The web app has an optional shared access code (`TRIAGE_ACCESS_CODE`):
unset means open, set means submissions must include it. It is a speed bump
against drive-by cost abuse, not real authentication or rate limiting - the
Anthropic Console spend cap is the actual backstop.

Not implemented: rate limiting (serverless instances are stateless, so an
in-process limiter would be ineffective), and history persistence on Vercel
(the filesystem is read-only; `_append_history` fails soft with a stderr note).

## Local vector search ("RAG") - optional, local only

`TRIAGE_RAG=1` adds a retrieval step before the model call. Nothing changes
when it is unset, and it is **never installed on Vercel**: its dependencies
(`faiss-cpu`, `sentence-transformers` -> PyTorch) are in `requirements-rag.txt`,
not `requirements.txt` / `pyproject.toml`, and every import is lazy.

1. **Knowledge base.** `KNOWN_FAILURES` (12 categories: S3 permission, schema
   drift, OOM, connection timeout, data-quality rejects, missing input,
   expired credentials, disk full, Avro/schema mismatch, type conversion,
   throttling, encoding) plus past triage results from the local history
   file, de-duplicated by content, live in a local FAISS index
   (`.triage_vectors/cases.faiss` = the vectors, `cases.json` = the texts in
   the same row order; gitignored, rebuilt on demand; only new or changed
   cases are re-embedded - see below).
2. **Embedding.** `all-MiniLM-L6-v2` via `sentence-transformers`: free, local, no
   API key. Vectors are L2-normalized so inner product = cosine similarity;
   distance is reported as `1 - similarity`.
3. **Query.** One short query per distinct error-ish line of the log (max 8,
   300 chars each), searched separately and merged by best distance, so each
   failure in a multi-failure log can pull in its own case. One embedding of
   the whole log would be wrong here: the model reads only ~256 word-pieces,
   so it would ignore everything after the opening lines. (The first version
   did exactly that and missed one of two failures; the error-line filter
   also had to match substrings so `ValueError` counts.)
4. **Prompt.** The top 3 (`RAG_TOP_K`) go into the user message before the log,
   labeled as background only, never as evidence. Both backends; not images.
5. **Fail-soft.** Missing packages, a model-download failure, or a bad index
   print a one-line stderr note and triage proceeds without context.

Known limits, stated plainly:
- **No clean similarity cutoff.** Right matches scored 0.44-0.68 and irrelevant
  filler 0.35-0.42, so any threshold either drops real matches or lets noise
  in. `RAG_MAX_DISTANCE` is loose; the prompt tells the model to ignore weak
  matches, which it can only mostly do.
- **Modest benefit on easy logs.** The sample logs are triaged correctly with
  or without it; retrieval earns its keep on recurring, house-specific failures
  whose past fixes are in the history/catalog.
- **History self-match.** Re-triaging a log already in the history retrieves
  its own earlier answer (similarity ~0.9), so the model sees its prior output
  as "similar case". Fine for recurring incidents, misleading when testing.
- **Cost:** the first run downloads a ~90 MB model, and each CLI run pays a few
  seconds to load it (the web app would pay once per process).
- **History is not de-noised:** a wrong past triage becomes a "known case".

### What the Chroma -> FAISS swap exposed

The retrieval was first built on Chroma and rebuilt on FAISS with the same
model. Rankings and scores were identical on every log compared (Chroma's
approximate HNSW and FAISS's exact search agree at this scale). What changed
is that FAISS is a search *library*, not a database, so everything Chroma did
behind `collection.query()` is now explicit code in `Triage.py`:

- **An index is only a matrix.** `index.search()` returns float scores and
  integer *row positions*. No text, no ids, no metadata. `cases.faiss` is
  exactly `n * 384 * 4` bytes plus a header; the texts live in `cases.json`,
  and `_open_index` keeps the two row-aligned. If they disagree (different
  counts, different model) the saved vectors are discarded, not trusted.
- **No upsert.** Adding a vector twice yields two rows; there is no "update by
  id". Incremental updates are done by `reconstruct`-ing the saved vectors of
  unchanged cases and embedding only the new ones, then rebuilding the flat
  index (instant at this size).
- **Cosine is your responsibility.** `IndexFlatIP` is a raw inner product. It
  equals cosine similarity only for unit-length vectors, so `_embed`
  normalizes and casts to float32 (FAISS rejects float64). all-MiniLM-L6-v2
  already normalizes internally (its pipeline ends in a `Normalize` module),
  so the explicit step is a guard for a future model swap, not a fix today.
  Without it a longer, less relevant vector outranks a better match (demoed:
  a 60%-relevant case stored at 2x length beat a perfect match).
- **You pick the search algorithm.** Chroma silently used HNSW, an
  *approximate* graph index. FAISS makes you choose. `IndexFlatIP` is exact
  brute force: right for tens to thousands of cases. Approximate indexes
  trade recall for speed only at ~100k+ vectors, and are tunable but not
  exact (on 20k synthetic clustered vectors HNSW recall@10 rose from 15% to
  82% as `efSearch` went 16 -> 256; synthetic high-dimensional data is a hard
  case, so read that as "approximate and tunable", not as typical numbers).
- **Score direction and padding.** FAISS returns similarity (higher = better)
  and pads with `-1` when asked for more results than exist; both are handled
  in `_retrieve_similar`.

Retrieval-quality finding (independent of the engine): a case can score low
against a clearly matching log line - S3 `AccessDenied` scored 0.41 against
the S3 catalog case. Stripping the timestamp/level/component prefix did not
fix it (0.41 -> 0.42), so it is this small model matching noisy log lines to
prose descriptions, not boilerplate dilution. Not changed.

## Screenshot ("image log") triage

Uploading a PNG/JPEG/GIF/WebP screenshot of a log triages it with the same
prompt, using the model's vision. **Default (`cli`) backend, no API key:** the
image is written to a private temp dir and `claude -p` runs there with only the
`Read` tool enabled (`--tools Read --add-dir <tmp>`), which lets it view the
image through the user's Claude subscription (`run_triage_via_claude_cli_image`).
The `api` backend (Vercel) sends it as a native image block instead.

- Images are recognized by **magic bytes** (`detect_image_media_type`), not
  filename or client-supplied content type, which can lie.
- The request is an image block plus a text block that tells the model to
  quote only text it can actually read, flag blurry/cut-off lines in `notes`,
  and return an empty `failures` list if the image isn't a log (verified: a
  plain blue square yields no invented failures).
- `MAX_IMAGE_BYTES` (4 MB) sits under Vercel's ~4.5 MB body limit and the
  API's 5 MB image limit; oversized images are rejected before any billed call.
- Text and image requests share `_call_api`, so error mapping, truncation
  detection, and schema validation are identical.
- The web app now also rejects non-text, non-image binary uploads (PDF, zip,
  and UTF-16 text, which contains NUL bytes) with a clear message instead of
  decoding them into gibberish. This also turns the UTF-16 gap below into an
  explicit error *for the web app*; the CLI path is unchanged.

Known limits: screenshot triage is a good first pass, not a transcript.
Small, blurry, or partly cropped text can be misread or elided (a live test
showed the model abbreviating one long evidence line with "..."), so verify
quoted evidence against the source before acting on it. Text inside an image
can also try to steer the model (prompt injection); the worst case is a
skewed report, since output is HTML-escaped and no tools are available to it.

## Log varieties

The script treats the log as opaque text and lets the model do all the
interpretation, so it already handles differently-shaped logs (line-oriented,
JSON-lines, embedded multi-line stack traces) without any format-specific
parsing. Two robustness gaps in the *file-handling* layer (not the model)
were found and addressed:

- **Non-UTF-8 encodings.** `open(log_path, encoding="utf-8")` used to crash
  with a raw, uncaught `UnicodeDecodeError` on any legacy-encoded log
  (Windows-1252/Latin-1 tools emitting an accented character, say). Fixed
  with a two-step read (`_read_log_file`): try UTF-8, fall back to Latin-1 -
  which can decode any byte sequence - and print a one-line stderr note when
  that fallback is used.
  - **Known gap, accepted as-is:** this two-step fallback only handles
    single-byte legacy encodings correctly. A **UTF-16-encoded log** doesn't
    trigger the fallback path at all, because Latin-1 decoding never raises
    - it just silently decodes the interleaved-null-byte UTF-16 bytes into
    mojibake, with no warning printed (the code only warns when UTF-8
    *fails*, and here Latin-1 "succeeds," just on the wrong data). The model
    would receive garbled text with no signal to the user that the root
    cause was encoding detection rather than a real content problem. Closing
    this needs a BOM check (`\xff\xfe` / `\xfe\xff` at the start of the file)
    before falling back to Latin-1 - not implemented, since it was flagged as
    an edge case and explicitly deferred rather than folded into that change.
- **Oversized logs.** No size guard existed. A very large log risks hitting
  OS argument-length limits (the whole prompt is passed as a single `claude
  -p` argument) or needlessly blowing through context/cost. Fixed with
  `_truncate_log_text`: caps at `MAX_LOG_CHARS` (200k), keeping a head+tail
  excerpt (a failure tends to show up near the start or the end of a run,
  not necessarily in a middle section that gets cut) with an explicit
  `[... N characters omitted ...]` marker embedded in the log text itself,
  plus a one-line stderr note.

New fixtures added to validate these and other varieties (`sample_json_lines.log`,
a JSON-lines structured log; `sample_stacktrace.log`, a multi-line Python
traceback embedded in an otherwise normal log; `sample_latin1.log`, a
genuinely Latin-1-encoded file) - none tracked in git, same as the other
`.log` fixtures.

## Persistence: session state vs. local state

Explicit design decision on what needs to survive a single run of this
script versus what only needs to exist within it:

- **Within one run only (discarded on exit):** the raw log text, the
  assembled prompt, the `claude` CLI subprocess and its JSON envelope
  response. None of this is useful once the report has been printed - there
  is no conversation to continue and no reason to keep it around.
- **Persisted across runs:** a compact record of each completed triage,
  appended to `.triage_history.jsonl` (one JSON object per line) via
  `_append_history()`: a UTC timestamp, the log path, the model used, and
  the full `Triage` result (all `failures` plus `notes`). This is a local,
  per-machine file - gitignored, not synced anywhere, human-inspectable
  with any JSON-lines tool.

This is deliberately just the persistence layer, not a retrieval/RAG loop:
nothing reads `.triage_history.jsonl` back in today. A natural next step
would be, before triaging a new log, to search this file for past runs with
a similar `failure_type`/`evidence` and pass a few in as few-shot context -
useful for recognizing a recurring failure faster or more consistently -
but that wasn't built here; only the persistence half was in scope.

Writing history is best-effort: `_append_history()` catches `OSError` (e.g.
an unwritable path) and prints a one-line stderr note rather than failing
the whole command - the user's report on stdout is the primary deliverable,
and losing one history line is far less disruptive than losing that.

**Known gap, accepted as-is:** the history file has no rotation, size cap,
or pruning - it grows unbounded, one line per run, forever. Fine for a
personal debugging tool run occasionally by hand; a real problem if this
ever ran unattended or very frequently (e.g. on every pipeline failure in
a busy system). Left unfixed since it wasn't asked for and would be scope
creep beyond "decide what persists and wire it up" - worth adding (e.g. cap
at the last N records, or prune by age) before this runs in any automated
context.

## Postgres workflow + dashboard - optional, local only

With `DATABASE_URL` set, each triage is also saved to a local Postgres so the
findings can be assigned, tracked and costed by teams. Unset, none of this
runs: the CLI, tests and the Vercel/Azure builds are unaffected.

- **Setup:** `pip install -r requirements-db.txt` (`psycopg` is imported
  lazily and deliberately not in `requirements.txt`, which the hosted builds
  install). `python seed_db.py` applies `schema.sql`, creates 3 teams and 2
  users per team (one `lead`, one `engineer`), and prints each new user's
  random password once. Only a salted scrypt hash is stored, so a lost
  password means deleting the user row and re-seeding. Re-running is safe;
  existing users are never overwritten.
- **Tables:** `teams` (with an hourly rate for costing), `users`,
  `triage_runs` (one per log), `triage_findings` (one per distinct root
  cause - the unit that gets triaged and tracked) and
  `finding_status_history` (audit trail).
- **Save hook:** `_append_history()` also calls `_record_to_db()` ->
  `triage_db.record_run()`, so the CLI, `local_server.py` and the hosted
  backends all share one path. It is best-effort like the JSONL history: a
  database failure prints a stderr note and never loses the report.
- **Routing is rule-based, not Claude-generated.** `triage_db.classify()`
  matches keywords in `failure_type`/`what_broke` to a category and owning
  team (`CATEGORY_RULES`, first match wins, fallback `other` ->
  Application Engineering). Priority is P1/high for the first failure (the
  run's blocker, per the prompt's ordering) and P2/medium for the rest.
  Estimated hours come from the category, cost is hours x the team's rate,
  and the default ETA is twice the estimate. These are placeholders a human
  edits in the dashboard. **Next step, not built:** have Claude return
  category, priority and an effort estimate per failure - that means
  extending the `Failure` model and prompt, and updating the existing tests.
- **Auth and permissions:** `POST /api/login` sets an HMAC-signed, HttpOnly,
  SameSite=Lax cookie (8h). The signing key is `TRIAGE_SECRET_KEY`, or a
  random per-process key, in which case restarting the server signs everyone
  out. Anyone signed in can read all findings; an engineer can edit only
  findings assigned to their team, and only a lead can reassign teams.
  Assignees must belong to the assigned team. Editing hours re-costs the
  finding; changing status writes a history row and sets or clears
  `resolved_at`.
- **Dashboard:** `web/dashboard.html`, served by `local_server.py` at
  `http://localhost:8000/dashboard.html`. It uses the session cookie, so it
  works same-origin only - it does not work from the Vercel-hosted page.
  Model-written text is rendered with `textContent`, never `innerHTML`.
- **Known gaps:** no login rate limiting or lockout; no password change or
  user management UI; plain `http` on localhost; no automated tests of the
  FastAPI routes beyond a manual smoke test (the `triage_db` logic is covered
  by `tests/test_triage_db.py` with a fake connection, not a real Postgres);
  `schema.sql` renames a pre-existing early-prototype `triage_runs` table to
  `triage_runs_old` instead of dropping it.

## Multi-failure fixture

`sample_pipeline.log` deliberately exercises the single-failure path only:
it has one root cause (missing `exchange_rate` column in `fx.daily_rates`)
plus downstream cascade lines that the prompt is told *not* to split out.
`sample_multi_failure.log` fills the gap: two independent root causes on
unrelated branches (an S3 `AccessDenied` on one branch, an unrelated
`MemoryError` during a dedupe step on another), used to verify the
multi-failure rendering path and to catch response-truncation bugs (see
below). Neither fixture is tracked in git - both fall under the repo's
`*.log` ignore rule and are local dev files only.

## History

- The response-render path and the request path were originally out of
  sync: `main()` asked for structured output but the tail of `main()` still
  printed raw text/JSON blocks instead of calling `print_report()`. Fixed by
  wiring `print_report(response.parsed_output)` in (before the later switch
  to the CLI).
- The anthropic SDK raises a bare `TypeError` (not `AuthenticationError`)
  when no credentials are configured at all, which was originally uncaught
  and crashed with a raw traceback. No longer applicable after the switch to
  the `claude` CLI, which fails via a normal non-zero exit / stderr message
  instead.
- `max_tokens` was briefly lowered from `16000` to `280`, which truncated
  the JSON response mid-field on any log with more than one failure
  (confirmed against `sample_multi_failure.log`, a `pydantic.ValidationError`
  crash) and was reverted. This class of bug (truncation on multi-failure
  logs) is exactly what `sample_multi_failure.log` exists to catch - run it
  after touching any token/length-related setting.
