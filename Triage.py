"""Read a pipeline log file and ask Claude what actually broke."""

import sys

import anthropic
from pydantic import BaseModel

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are a data pipeline on-call engineer triaging a failed run.

Given a raw pipeline log, break the run down into its distinct failures. A
distinct failure is its own root cause - a separate thing an on-call engineer
would have to fix. Do NOT split one root cause into several entries just
because it produced many log lines, and do NOT merge two unrelated root causes
into one entry because they happened in the same run.

For each distinct failure, give:
- failure_type: a short label naming the kind of failure, e.g.
  "S3 permission denied" or "upstream schema drift".
- what_broke: the real root failure in plain English, not downstream noise.
- evidence: the specific lines or values in the log that point to it.
- next_step: the most likely next step to fix or confirm it.

Order the failures by what to look at first - the one most likely to be the
run's real blocker goes first. Downstream failures that only happened because
an earlier failure did are not distinct: fold them into that failure's entry.

Use `notes` for anything that does not belong to a single failure: how the
failures relate, or an ambiguous root cause where you should name the competing
possibilities. Leave `notes` as an empty string if you have nothing to add.

Be concise and concrete. Skip anything the log does not support."""


class Failure(BaseModel):
    failure_type: str
    what_broke: str
    evidence: str
    next_step: str


class Triage(BaseModel):
    failures: list[Failure]
    notes: str


def print_report(triage: Triage) -> None:
    if len(triage.failures) > 1:
        print(f"{len(triage.failures)} distinct failures found.\n")

    for i, failure in enumerate(triage.failures, start=1):
        heading = failure.failure_type
        if len(triage.failures) > 1:
            heading = f"{i}. {heading}"
        print(heading)
        print("-" * len(heading))
        print(f"What broke: {failure.what_broke}")
        print(f"Evidence:   {failure.evidence}")
        print(f"Next step:  {failure.next_step}")
        print()

    if triage.notes:
        print(f"Notes: {triage.notes}")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: python {sys.argv[0]} <log-file>", file=sys.stderr)
        return 1

    log_path = sys.argv[1]
    try:
        with open(log_path, encoding="utf-8") as f:
            log_text = f.read()
    except OSError as e:
        print(f"could not read {log_path}: {e}", file=sys.stderr)
        return 1

    if not log_text.strip():
        print(f"{log_path} is empty - nothing to triage.", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Pipeline log from `{log_path}`:\n\n<log>\n{log_text}\n</log>",
                }
            ],
            output_format=Triage,
        )
    except anthropic.AuthenticationError:
        print("auth failed - set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"API error ({e.status_code}): {e.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("network error reaching the Anthropic API.", file=sys.stderr)
        return 1

    print_report(response.parsed_output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
