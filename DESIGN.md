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
4. Run `claude -p "<prompt>" --model claude-sonnet-5 --output-format json`
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
