# Pipeline Log Triage - design notes

## What the script does today

`Triage.py` is a single-shot CLI. It reads one pipeline log file off disk, sends
the whole file to Claude in one Messages API request, and prints the triage back
to stdout. There is no loop, no tool use, no state between runs.

Flow:

1. Validate argv - exactly one argument, the log path.
2. Read the file as UTF-8; bail if unreadable or blank after `strip()`.
3. `anthropic.Anthropic()` with credentials resolved from the environment
   (`ANTHROPIC_API_KEY`, or an `ant auth login` profile).
4. One `client.messages.parse()` call against `claude-opus-5`, `max_tokens=16000`,
   the on-call-engineer system prompt, and `output_format=Triage`.
5. Print the result.

The system prompt asks Claude to break the run into **distinct failures** -
one entry per root cause. Downstream noise (tasks that only failed because an
earlier task did) gets folded into the entry for the failure that caused it,
rather than becoming its own entry.

## Input format

A plain-text log file, passed as the only positional argument:

```
python triage.py sample_pipeline.log
```

No format is assumed beyond "text a pipeline emitted." `sample_pipeline.log` is
the reference shape: `TIMESTAMP LEVEL COMPONENT MESSAGE`, one event per line.
The file is sent verbatim, wrapped in a `<log>` tag with its path, and is never
truncated or chunked.

## Output format

**Structured shape requested from the model** (`Triage`, enforced by structured
outputs, so the response is always schema-valid):

| Field | Type | Meaning |
| --- | --- | --- |
| `failures` | list of `Failure` | one entry per distinct root cause, most-likely-blocker first |
| `failures[].failure_type` | str | short label, e.g. `"upstream schema drift"` |
| `failures[].what_broke` | str | the root failure in plain English |
| `failures[].evidence` | str | the log lines/values that point to it |
| `failures[].next_step` | str | next action to fix or confirm |
| `notes` | str | cross-failure context or a named ambiguity; `""` when there is nothing to add |

**What actually reaches stdout right now:** the raw JSON of that object. See the
limitation below.

Exit codes: `0` on success; `1` for a usage error, an unreadable or empty log
file, or an auth / API-status / connection failure (each with a one-line message
on stderr).

## Limitation hit today

**The request path and the print path are out of sync, so the feature does not
actually render.** `main()` now asks for structured output via
`messages.parse(output_format=Triage)`, but the tail of `main()` is still the
original text-block loop:

```python
for block in response.content:
    if block.type == "text":
        print(block.text)
```

With structured outputs that block *is* the serialized JSON, so the script
prints a JSON blob instead of a report. `print_report()` - which does the
per-failure sectioning and the `"N distinct failures found."` header, i.e. the
whole point of the change - is defined at `triage.py:48` and never called, and
`response.parsed_output` is never read. Wiring it up is a two-line change to the
end of `main()`.

Consequence: nothing has been verified end-to-end - no API call has been made
from this working tree. And the one multi-error fixture we'd want in order to
verify it does not exist: `sample_pipeline.log` contains a single root cause
(missing `exchange_rate` column in `fx.daily_rates`) plus two downstream
cascade lines, which is the case the prompt is told *not* to split. So even once
the renderer is wired in, the sample in the repo exercises the one-failure path
only.
