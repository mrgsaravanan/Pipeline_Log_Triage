"""Read a pipeline log file and ask Claude what actually broke.

Locally this routes through the `claude` CLI (a Claude Code / Claude
subscription) rather than a billed API key. In a hosted environment (Vercel,
or TRIAGE_BACKEND=api) it calls the Anthropic API instead, since a serverless
function can't hold an interactive `claude login` session.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError, field_validator

CLAUDE_CLI = "claude"
MODEL = "claude-haiku-4-5"
CLI_TIMEOUT_SECONDS = 180

# Hosted (API) backend. The timeout stays under the 60s Vercel maxDuration set
# in vercel.json, so a slow call fails with our message rather than a platform
# kill. max_tokens is generous: a too-small value once truncated a multi-failure
# reply mid-JSON (see DESIGN.md history), and we only pay for tokens generated.
API_TIMEOUT_SECONDS = 50.0
API_MAX_TOKENS = 8192

# Screenshot ("image log") support, hosted/API backend only. The cap sits under
# Vercel's ~4.5 MB request-body limit (and the API's 5 MB per-image limit) so an
# oversized image gets our clear message instead of a platform error.
MAX_IMAGE_BYTES = 4_000_000

# The only thing that persists between separate runs of this script. Every
# other piece of state - the raw log text, the built prompt, the claude CLI
# subprocess and its response - lives only within one run and is discarded
# when the process exits; there's no reason for any of that to survive past
# printing the report. This file is a plain append-only log of completed
# triages: local, human-inspectable, and not synced anywhere (see
# .gitignore). It is not yet read back in anywhere (no retrieval/few-shot
# step) - it's the persistence layer a future retrieval step would build on.
HISTORY_FILE = ".triage_history.jsonl"

# Keep the log within a size that's safe to pass as a single CLI argument and
# comfortably inside the model's context window. Bias toward keeping both
# ends of the file: a pipeline failure tends to show up either near the
# start (a connection/auth failure) or the end (the task that actually
# broke), so a head+tail excerpt is more useful than a head-only one.
MAX_LOG_CHARS = 200_000
TRUNCATE_HEAD_CHARS = 120_000
TRUNCATE_TAIL_CHARS = 60_000

# --- Optional local vector search ("RAG") -----------------------------------
# Off by default; enable with TRIAGE_RAG=1. Before triaging, embed the log's
# error lines with a free local model and pull the most similar known failure
# cases from a local FAISS index into the prompt as reference. Runs only
# on a developer machine: the dependencies (faiss-cpu + sentence-transformers,
# which pulls in PyTorch) live in requirements-rag.txt, NOT in requirements.txt
# or pyproject.toml, so the Vercel build never installs them. Every import is
# lazy and every failure is soft - retrieval can never break a triage.
RAG_DIR = ".triage_vectors"
RAG_INDEX_NAME = "cases.faiss"  # the vectors, and nothing else (FAISS stores no text)
RAG_META_NAME = "cases.json"  # the texts, in the same row order as the vectors
RAG_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
RAG_TOP_K = 3
# Cosine distance (0 identical .. 2 opposite). Anything farther than this is
# noise, not a similar case, and is dropped rather than shown to the model.
RAG_MAX_DISTANCE = 0.85
# all-MiniLM-L6-v2 only reads its first 256 word-pieces (~1,000 characters), so
# one embedding of a whole log would ignore most of it. Instead each distinct
# error line becomes its own short query and the results are merged, which also
# lets every failure in a multi-failure log pull in its own similar case.
RAG_QUERY_LINE_CHARS = 300
RAG_MAX_QUERIES = 8

# Known failure categories: a description plus the log phrasing each one
# typically produces and the usual fix. Extend this list, or let the local
# history file (past triage results) supplement it automatically.
KNOWN_FAILURES = [
    {
        "id": "s3-access-denied",
        "title": "S3 / object storage permission denied",
        "text": (
            "A task cannot read or write an object in cloud storage: AccessDenied, "
            "403 Forbidden, botocore ClientError on GetObject or PutObject. Usually "
            "an IAM role, bucket policy, or KMS key policy changed or lacks the "
            "permission. Check the role attached to the run against the bucket and "
            "key prefix."
        ),
    },
    {
        "id": "schema-drift-missing-column",
        "title": "Upstream schema drift (missing or renamed column)",
        "text": (
            "A transform fails with KeyError, column not found, or invalid column "
            "name because an upstream table or file dropped, renamed, or retyped a "
            "column. Compare the current upstream schema with what the task "
            "expects; restore or remap the column, then re-run."
        ),
    },
    {
        "id": "out-of-memory",
        "title": "Out of memory in a transform",
        "text": (
            "MemoryError, OutOfMemoryError, or unable to allocate N GiB for an "
            "array. A join, cross product, or full-data load is far larger than "
            "the worker memory. Chunk or stream the processing, fix the exploding "
            "join, or raise the memory limit."
        ),
    },
    {
        "id": "connection-timeout",
        "title": "Database or service connection timeout",
        "text": (
            "Connection refused, connection timed out, could not connect to server, "
            "or network unreachable while opening a source or sink. The database "
            "or service is down, overloaded, blocked by a firewall or VNet rule, or "
            "the host name changed. Check reachability from the runtime."
        ),
    },
    {
        "id": "data-quality-rejects",
        "title": "Data quality: rows rejected for invalid values",
        "text": (
            "A stage logs N rows rejected, invalid value for column, or failed "
            "validation, while the job still finishes. Malformed emails, dates, or "
            "nulls in the source. Inspect the reject output and decide whether to "
            "fix the source or add a cleansing rule."
        ),
    },
    {
        "id": "missing-input-file",
        "title": "Expected input file or partition missing",
        "text": (
            "File not found, no such key, path does not exist, or zero rows for the "
            "run date. The upstream job has not delivered its output yet or wrote "
            "to a different path. Check the producer's schedule and output "
            "location; add a wait or dependency."
        ),
    },
    {
        "id": "credential-expired",
        "title": "Expired or invalid credentials",
        "text": (
            "401 Unauthorized, authentication failed, token expired, invalid client "
            "secret, or login failed for user. A service principal secret, password, "
            "or access token expired or was rotated. Renew the credential in the "
            "secret store and confirm the pipeline reads the new version."
        ),
    },
    {
        "id": "disk-full",
        "title": "Disk or storage full",
        "text": (
            "No space left on device, disk quota exceeded, or failed to write "
            "temporary or spill files. The worker's local disk or a staging area "
            "filled up. Clean old temp files, enlarge the volume, or reduce spill."
        ),
    },
    {
        "id": "serialization-schema-mismatch",
        "title": "Serialization / schema registry mismatch (Avro, Protobuf)",
        "text": (
            "SchemaResolutionError, deserialization failed, or reader schema is "
            "missing a field from the writer schema on a stream or topic. Producer "
            "and consumer schema versions diverged. Update the consumer schema to "
            "the producer's version, then resume from the failed offset."
        ),
    },
    {
        "id": "type-conversion",
        "title": "Type conversion error on bad numeric data",
        "text": (
            "ValueError: could not convert string to float, invalid literal for "
            "int, or cast failure, often from a placeholder such as N/A, empty "
            "string, or NULL in a numeric column. Clean or coerce the values "
            "upstream, or handle them explicitly before the calculation."
        ),
    },
    {
        "id": "throttling",
        "title": "API throttling / rate limit exceeded",
        "text": (
            "429 Too Many Requests, rate limit exceeded, throttled, or request "
            "quota exhausted from a source API or storage account. Too many "
            "parallel copies or requests. Lower concurrency, add retry with "
            "backoff, or raise the quota."
        ),
    },
    {
        "id": "encoding-error",
        "title": "Character encoding mismatch",
        "text": (
            "UnicodeDecodeError, invalid byte sequence, or garbled characters when "
            "reading a file. The source is in a legacy encoding such as "
            "Windows-1252 or Latin-1 but is read as UTF-8. Declare the correct "
            "encoding or have the producer re-export as UTF-8."
        ),
    },
]

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
- severity: one of "critical" (pipeline down or data loss), "high" (run blocked),
  "medium" (degraded or recoverable) or "low" (cosmetic or self-healing).
- confidence: a number from 0 to 1 for how sure you are of this root cause,
  given only what the log shows.
- suggested_fix: a concrete fix as a command, config change or code patch if
  the log supports one, otherwise an empty string. It is a suggestion for a
  human to review, so never claim it has been applied.

Order the failures by what to look at first - the one most likely to be the
run's real blocker goes first. Downstream failures that only happened because
an earlier failure did are not distinct: fold them into that failure's entry.

Use `notes` for anything that does not belong to a single failure: how the
failures relate, or an ambiguous root cause where you should name the competing
possibilities. Leave `notes` as an empty string if you have nothing to add.

Be concise and concrete. Skip anything the log does not support."""

JSON_RESPONSE_INSTRUCTIONS = """Respond with ONLY a single JSON object - no prose, no markdown
code fences, no commentary before or after it - matching exactly this shape:

{
  "failures": [
    {
      "failure_type": "string",
      "what_broke": "string",
      "evidence": "string",
      "next_step": "string",
      "severity": "critical | high | medium | low",
      "confidence": 0.0,
      "suggested_fix": "string"
    }
  ],
  "notes": "string"
}"""


IMAGE_LOG_INSTRUCTIONS = (
    "The pipeline log is in the attached screenshot. Read the text in it "
    "carefully and triage it as instructed. Quote evidence only from text you "
    "can actually read - if a line is blurry, cut off, or ambiguous, say so in "
    "`notes` rather than guessing. If the image does not contain a pipeline "
    "log, return an empty failures list and say what it shows in `notes`."
)


SEVERITIES = ("critical", "high", "medium", "low")


class Failure(BaseModel):
    failure_type: str
    what_broke: str
    evidence: str
    next_step: str
    # Optional so older stored reports and terse model output still validate.
    severity: str = "medium"
    confidence: float = 0.5
    suggested_fix: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _known_severity(cls, v: object) -> str:
        v = str(v).strip().lower()
        return v if v in SEVERITIES else "medium"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: object) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


class Triage(BaseModel):
    failures: list[Failure]
    notes: str


class ClaudeCliError(RuntimeError):
    """The claude CLI failed, or returned something we can't use."""


class ClaudeApiError(ClaudeCliError):
    """The hosted (Anthropic API) backend failed. Subclasses ClaudeCliError so
    callers that already handle backend failures keep working unchanged."""


def _read_log_file(log_path: str) -> tuple[str, bool]:
    """Read a log file, tolerating a non-UTF-8 encoding.

    Most pipeline logs are UTF-8, but some tools emit legacy encodings
    (Windows-1252, Latin-1) with the odd curly quote or accented character.
    Fall back to Latin-1 - which can decode any byte sequence - rather than
    crashing with a raw UnicodeDecodeError. A genuinely unreadable file
    (missing, no permission) is a separate OSError case the caller handles.

    Returns (text, used_fallback_encoding).
    """
    with open(log_path, "rb") as f:
        raw = f.read()
    text, fallback = decode_log_bytes(raw)
    return text, fallback


def decode_log_bytes(raw: bytes) -> tuple[str, bool]:
    """Decode log bytes: UTF-16 (BOM), UTF-8, else Latin-1. Returns (text, used_fallback)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), False
    try:
        return raw.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        return raw.decode("latin-1"), True


def _load_image(log_path: str) -> tuple[str, bytes] | None:
    """(media_type, bytes) if the file is a supported image, else None.

    Reads at most MAX_IMAGE_BYTES + 1 bytes so an enormous file can't be pulled
    into memory just to be rejected. An unreadable file also returns None; the
    text path that follows reports the OSError with its usual message.
    """
    try:
        with open(log_path, "rb") as f:
            header = f.read(16)
            media_type = detect_image_media_type(header)
            if media_type is None:
                return None
            return media_type, header + f.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None


def _truncate_log_text(log_text: str) -> tuple[str, bool]:
    """Cap log size with a head+tail excerpt. Returns (text, was_truncated)."""
    if len(log_text) <= MAX_LOG_CHARS:
        return log_text, False

    head = log_text[:TRUNCATE_HEAD_CHARS]
    tail = log_text[-TRUNCATE_TAIL_CHARS:] if TRUNCATE_TAIL_CHARS else ""
    middle = log_text[len(head):len(log_text) - len(tail)]
    kept = _error_lines(middle, MAX_LOG_CHARS - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS)
    omitted = len(middle) - len(kept)
    marker = f"\n\n[... {omitted:,} characters omitted from the middle of this log ...]\n\n"
    if kept:
        marker = (f"\n\n[... middle of log omitted; error-like lines kept below, "
                  f"{omitted:,} other characters dropped ...]\n{kept}\n\n")
    return head + marker + tail, True


_ERROR_LINE = re.compile(r"error|fail|exception|fatal|traceback|denied|timeout|timed out|"
                         r"killed|oom|panic", re.IGNORECASE)


def _error_lines(text: str, budget: int) -> str:
    """Error-like lines from the omitted middle of a huge log, up to `budget` chars."""
    kept, used = [], 0
    for line in text.splitlines():
        if _ERROR_LINE.search(line):
            line = line[:400]
            if used + len(line) + 1 > budget:
                break
            kept.append(line)
            used += len(line) + 1
    return "\n".join(kept)


def _strip_code_fence(text: str) -> str:
    """Undo a ```/```json wrapper if the model added one despite being told not to."""
    text = text.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        lines = lines[1:-1]
    else:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _log_message(log_path: str, log_text: str, similar_context: str = "") -> str:
    prefix = f"{similar_context}\n\n" if similar_context else ""
    return f"{prefix}Pipeline log from `{log_path}`:\n\n<log>\n{log_text}\n</log>"


def _parse_triage_text(text: str) -> Triage:
    """Validate the model's JSON reply (either backend) against the Triage schema."""
    try:
        return Triage.model_validate_json(_strip_code_fence(text))
    except ValidationError as e:
        raise ClaudeCliError(f"claude's response did not match the expected schema: {e}") from e


def _triage_backend() -> str:
    """'cli' (local, subscription) or 'api' (hosted, billed API key).

    TRIAGE_BACKEND wins if set. Otherwise the hosted case is detected via
    Vercel's automatic VERCEL env var, so local dev stays CLI-based with no
    configuration and a Vercel deploy needs no extra switch.
    """
    explicit = os.environ.get("TRIAGE_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    return "api" if os.environ.get("VERCEL") else "cli"


def _rag_enabled() -> bool:
    return os.environ.get("TRIAGE_RAG", "").strip().lower() in ("1", "true", "yes", "on")


# Substring matches on purpose: "ValueError" and "MemoryError" must count, which
# a \berror\b word-boundary match would miss.
_SIGNAL_LINE = re.compile(
    r"error|exception|fatal|critical|traceback|fail|denied|refused|timeout|timed out|"
    r"rejected|invalid|unable|cannot|could not|missing|not found|no space",
    re.IGNORECASE,
)


def _rag_query_texts(log_text: str) -> list[str]:
    """One short query per distinct error-ish line (capped), else the log's head.

    Falls back to the first lines when nothing looks like an error, so a quiet
    log still gets a (weaker) query rather than none.
    """
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    signal = list(dict.fromkeys(ln for ln in lines if _SIGNAL_LINE.search(ln)))
    if not signal:
        return ["\n".join(lines[:5])[:RAG_QUERY_LINE_CHARS]] if lines else []
    return [ln[:RAG_QUERY_LINE_CHARS] for ln in signal[:RAG_MAX_QUERIES]]


def _history_cases() -> list[dict]:
    """Past triage results from the local history file, as extra searchable cases.

    De-duplicated by content (re-running the same log would otherwise fill the
    top-3 with copies of itself); malformed lines are skipped, a missing file
    is simply no history.
    """
    cases: dict[str, dict] = {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    for line in lines:
        try:
            record = json.loads(line)
            failures = record["triage"]["failures"]
            stamp = record["timestamp"]
        except (ValueError, KeyError, TypeError):
            continue
        for i, failure in enumerate(failures):
            try:
                title = f"Past run: {failure['failure_type']}"
                text = (
                    f"{failure['what_broke']} Evidence: {failure['evidence']} "
                    f"Next step: {failure['next_step']}"
                )
            except (KeyError, TypeError):
                continue
            cases[text] = {"id": f"hist-{stamp}-{i}", "title": title, "text": text[:800],
                           "source": "history"}
    return list(cases.values())


def _case_document(case: dict) -> str:
    """The exact text that gets embedded (and that identifies a case on disk)."""
    return f"{case['title']}. {case['text']}"


def _load_embedder():
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(RAG_EMBEDDING_MODEL)


def _embed(embedder, texts: list[str]):
    """Texts -> float32 matrix of unit-length vectors, one row per text.

    Two things FAISS will not do for you. It only accepts float32. And the
    similarity we search with is a raw inner product, which equals cosine
    similarity only when every vector has length 1 - so normalize here. (This
    particular model already normalizes internally; asking again is harmless
    and keeps the index correct if the model is ever swapped for one that
    doesn't.)
    """
    import numpy as np

    vectors = embedder.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return np.asarray(vectors, dtype="float32")


def _saved_vectors() -> dict[str, "object"]:
    """{document text: its saved vector} from the last-written index, or {}.

    FAISS's IndexFlat keeps the raw vectors, so `reconstruct` hands them back
    and only genuinely new texts need embedding. Everything that could make the
    saved vectors untrustworthy returns {} (=> re-embed all): a missing or
    unreadable file, a different embedding model, or the two files disagreeing
    on how many cases they hold.
    """
    import faiss

    try:
        index = faiss.read_index(os.path.join(RAG_DIR, RAG_INDEX_NAME))
        with open(os.path.join(RAG_DIR, RAG_META_NAME), encoding="utf-8") as f:
            meta = json.load(f)
        documents = meta["documents"]
        if meta["model"] != RAG_EMBEDDING_MODEL or index.ntotal != len(documents):
            return {}
        return {doc: index.reconstruct(row) for row, doc in enumerate(documents)}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return {}


def _open_index(cases: list[dict]):
    """(embedder, FAISS index) whose row i is cases[i].

    FAISS has no ids, metadata, upsert, or persistence-of-text: an index is just
    a matrix that answers "which rows are nearest". So this function is the
    bookkeeping a vector database does behind its collection API: keep the case list
    and the matrix row-aligned, re-embed only what changed, and persist both.
    """
    import faiss  # first, so a missing package fails before the slow model load
    import numpy as np

    embedder = _load_embedder()
    documents = [_case_document(c) for c in cases]

    saved = _saved_vectors()
    previous_documents = list(saved)
    new_documents = [d for d in dict.fromkeys(documents) if d not in saved]
    if new_documents:
        for doc, vector in zip(new_documents, _embed(embedder, new_documents)):
            saved[doc] = vector

    matrix = np.stack([saved[d] for d in documents])
    # Exact (brute-force) inner-product search. At tens to thousands of cases
    # this is instant and *exact*; approximate indexes (HNSW, IVF, PQ) only pay
    # off around 100k+ vectors, and a default HNSW index would be solving a scale
    # problem this project does not have.
    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(matrix)

    if documents != previous_documents:
        os.makedirs(RAG_DIR, exist_ok=True)
        faiss.write_index(index, os.path.join(RAG_DIR, RAG_INDEX_NAME))
        with open(os.path.join(RAG_DIR, RAG_META_NAME), "w", encoding="utf-8") as f:
            json.dump({"model": RAG_EMBEDDING_MODEL, "documents": documents}, f)
    return embedder, index


def _retrieve_similar(queries: list[str]) -> list[dict]:
    """Top-k most similar known cases from a local FAISS index.

    Each query is searched separately; a case's score is its best (smallest)
    distance across queries. Returns dicts with title/text/source/distance,
    nearest first, dropping anything beyond RAG_MAX_DISTANCE. Raises on any
    failure (missing packages, model download, disk); callers go through
    _similar_cases_for, which turns that into a soft skip.
    """
    if not queries:
        return []

    cases = [{**c, "source": "catalog"} for c in KNOWN_FAILURES] + _history_cases()
    embedder, index = _open_index(cases)

    # scores/rows are (n_queries, k) arrays. `rows` are integer positions in the
    # matrix, not case ids: cases[row] is how we get back to the text.
    scores, rows = index.search(_embed(embedder, queries), min(RAG_TOP_K, index.ntotal))

    best: dict[int, dict] = {}
    for query_scores, query_rows in zip(scores, rows):
        for score, row in zip(query_scores, query_rows):
            if row < 0:  # FAISS pads with -1 when it has fewer than k results
                continue
            # Inner product of unit vectors is cosine *similarity* (higher is
            # better); convert to cosine *distance* so RAG_MAX_DISTANCE and the
            # nearest-first ordering keep their meaning.
            distance = max(0.0, 1.0 - float(score))
            if distance <= RAG_MAX_DISTANCE and (
                row not in best or distance < best[row]["distance"]
            ):
                case = cases[row]
                best[row] = {"title": case["title"], "text": case["text"],
                             "source": case["source"], "distance": distance}
    return sorted(best.values(), key=lambda c: c["distance"])[:RAG_TOP_K]


def _format_similar_cases(similar: list[dict]) -> str:
    """The prompt block that hands the retrieved cases to the model."""
    if not similar:
        return ""
    entries = "\n".join(
        f"{i}. {c['title']} (similarity {1 - c['distance']:.2f})\n   {c['text']}"
        for i, c in enumerate(similar, start=1)
    )
    return (
        "Similar past cases from the knowledge base (found by vector search, most "
        "similar first). Use them only as background on likely causes and fixes, and "
        "only where they genuinely match this log. Do not force a match, and never "
        "cite them as evidence - evidence must come from the log below.\n\n"
        f"{entries}"
    )


def _similar_cases_for(log_text: str) -> str:
    """Prompt context for this log, or "" if RAG is off or anything goes wrong."""
    if not _rag_enabled():
        return ""
    try:
        similar = _retrieve_similar(_rag_query_texts(log_text))
    except Exception as e:  # noqa: BLE001 - retrieval is best-effort by design
        print(f"note: vector search skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return ""

    if similar:
        matched = ", ".join(f"{c['title']} ({1 - c['distance']:.2f})" for c in similar)
        print(f"note: vector search matched: {matched}", file=sys.stderr)
    else:
        print("note: vector search found no sufficiently similar cases", file=sys.stderr)
    return _format_similar_cases(similar)


def detect_image_media_type(data: bytes) -> str | None:
    """Identify PNG/JPEG/GIF/WebP by magic bytes (filenames and client-supplied
    content types can lie; the bytes can't). None means "not a supported image"."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def run_triage(log_path: str, log_text: str) -> Triage:
    """Triage a text log via whichever backend this environment uses."""
    backend = _triage_backend()
    if backend not in ("api", "cli"):
        raise ClaudeCliError(f"unknown TRIAGE_BACKEND {backend!r} (expected 'cli' or 'api')")
    similar_context = _similar_cases_for(log_text)
    if backend == "api":
        return run_triage_via_api(log_path, log_text, similar_context)
    return run_triage_via_claude_cli(log_path, log_text, similar_context)


def run_triage_image(image_name: str, image_bytes: bytes, media_type: str) -> Triage:
    """Triage a screenshot of a log via whichever backend this environment uses.

    The default (`cli`) path goes through the user's Claude subscription: the image
    is written to a temp file and the `claude` CLI reads it with its Read tool.
    """
    backend = _triage_backend()
    if backend == "api":
        return run_triage_via_api_image(image_name, image_bytes, media_type)
    if backend == "cli":
        return run_triage_via_claude_cli_image(image_name, image_bytes, media_type)
    raise ClaudeCliError(f"unknown TRIAGE_BACKEND {backend!r} (expected 'cli' or 'api')")


def _call_api(user_content) -> Triage:
    """Send one user turn to the Anthropic API and parse the JSON reply.

    Shared by the text and image paths so both get identical error mapping,
    truncation detection, and schema validation. `anthropic` is imported lazily
    so CLI-only local use never needs it at import time.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ClaudeApiError("ANTHROPIC_API_KEY is not set in this environment")

    import anthropic

    client = anthropic.Anthropic(timeout=API_TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=API_MAX_TOKENS,
            system=f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}",
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError as e:
        raise ClaudeApiError("Anthropic API rejected the API key (check ANTHROPIC_API_KEY)") from e
    except anthropic.APIStatusError as e:
        raise ClaudeApiError(f"Anthropic API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ClaudeApiError("could not reach the Anthropic API (network error)") from e

    if response.stop_reason == "max_tokens":
        # A cut-off reply is truncated JSON - fail clearly rather than with a parse error.
        raise ClaudeApiError("response was truncated at max_tokens; raise API_MAX_TOKENS")

    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise ClaudeApiError("Anthropic API returned no text")
    return _parse_triage_text(text)


def run_triage_via_api(log_path: str, log_text: str, similar_context: str = "") -> Triage:
    """Ask Claude via the Anthropic API (billed ANTHROPIC_API_KEY) - for hosted use.

    Same prompt as the CLI path; only the transport differs.
    """
    return _call_api(_log_message(log_path, log_text, similar_context))


def run_triage_via_api_image(image_name: str, image_bytes: bytes, media_type: str) -> Triage:
    """Triage a screenshot of a log: the image plus the same triage prompt."""
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": f"Screenshot `{image_name}`. {IMAGE_LOG_INSTRUCTIONS}"},
    ]
    return _call_api(content)


def _run_claude_cli(prompt: str, extra_args: list[str] | None = None,
                    cwd: str | None = None) -> Triage:
    """Run `claude -p` (subscription-billed) and parse its JSON reply into a Triage."""
    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt, "--model", MODEL, "--output-format", "json",
         *(extra_args or [])],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        cwd=cwd,
    )

    if result.returncode != 0:
        raise ClaudeCliError(
            result.stderr.strip() or f"claude exited with status {result.returncode}"
        )

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"could not parse claude CLI output as JSON: {e}") from e

    if envelope.get("is_error"):
        raise ClaudeCliError(str(envelope.get("result") or "claude reported an error"))

    text = envelope.get("result")
    if not text:
        raise ClaudeCliError("claude CLI returned no result text")

    return _parse_triage_text(text)


def run_triage_via_claude_cli(
    log_path: str, log_text: str, similar_context: str = ""
) -> Triage:
    """Ask the claude CLI to triage a log, routed through the user's subscription."""
    prompt = (
        f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}\n\n"
        f"{_log_message(log_path, log_text, similar_context)}"
    )
    return _run_claude_cli(prompt)


_IMAGE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                 "image/webp": ".webp"}


def run_triage_via_claude_cli_image(
    image_name: str, image_bytes: bytes, media_type: str
) -> Triage:
    """Triage a screenshot through the user's subscription (no API key).

    `claude -p` cannot take an image on stdin, but its Read tool can view image
    files. So the image goes to a private temp dir, and the CLI runs there with
    only the Read tool enabled and access to only that directory.
    """
    with tempfile.TemporaryDirectory(prefix="triage-shot-") as tmp:
        path = os.path.join(tmp, "screenshot" + _IMAGE_SUFFIX.get(media_type, ".png"))
        with open(path, "wb") as f:
            f.write(image_bytes)
        prompt = (
            f"{SYSTEM_PROMPT}\n\n{JSON_RESPONSE_INSTRUCTIONS}\n\n"
            f"Use the Read tool to view the screenshot at {path} (originally named "
            f"`{image_name}`). {IMAGE_LOG_INSTRUCTIONS}"
        )
        return _run_claude_cli(
            prompt,
            ["--tools", "Read", "--allowedTools", "Read", "--add-dir", tmp],
            cwd=tmp,
        )


def _append_history(log_path: str, triage: Triage) -> None:
    """Best-effort: append this run's result to the local history file.

    A failure to write history is reported but does not fail the triage
    itself - the user already has their report on stdout, and losing one
    history line is much less disruptive than losing the report they asked
    for.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "log_path": log_path,
        "model": MODEL,
        "triage": triage.model_dump(),
    }
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"note: could not write to history file {HISTORY_FILE}: {e}", file=sys.stderr)
    _record_to_db(log_path, triage)


def _record_to_db(log_path: str, triage: Triage) -> None:
    """Also save the run to Postgres when DATABASE_URL is set (see triage_db.py).

    Imported lazily so the hosted builds, which don't ship triage_db, are unaffected.
    """
    try:
        from triage_db import record_run
    except ImportError:
        return
    record_run(log_path, triage, MODEL)


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
        print(f"Severity:   {failure.severity} (confidence {failure.confidence:.0%})")
        print(f"Next step:  {failure.next_step}")
        if failure.suggested_fix:
            print(f"Suggested fix (review before running): {failure.suggested_fix}")
        print()

    if triage.notes:
        print(f"Notes: {triage.notes}")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: python {sys.argv[0]} <log-file>", file=sys.stderr)
        return 1

    log_path = sys.argv[1]
    image = _load_image(log_path)
    if image is not None:
        media_type, image_bytes = image
        if len(image_bytes) > MAX_IMAGE_BYTES:
            print(
                f"{log_path} is larger than {MAX_IMAGE_BYTES / 1e6:.1f} MB; "
                "screenshots above that size are not supported.",
                file=sys.stderr,
            )
            return 1

        def job() -> Triage:
            return run_triage_image(log_path, image_bytes, media_type)
    else:
        try:
            log_text, used_fallback_encoding = _read_log_file(log_path)
        except OSError as e:
            print(f"could not read {log_path}: {e}", file=sys.stderr)
            return 1

        if used_fallback_encoding:
            print(
                f"note: {log_path} is not valid UTF-8; decoded as Latin-1 instead. "
                "Some characters may be misrendered.",
                file=sys.stderr,
            )

        if not log_text.strip():
            print(f"{log_path} is empty - nothing to triage.", file=sys.stderr)
            return 1

        log_text, was_truncated = _truncate_log_text(log_text)
        if was_truncated:
            print(
                f"note: {log_path} is large; sending a truncated head+tail "
                "excerpt to the model instead of the full file.",
                file=sys.stderr,
            )

        def job() -> Triage:
            return run_triage(log_path, log_text)

    try:
        triage = job()
    except FileNotFoundError:
        print(
            "claude CLI not found - install Claude Code and run `claude login`.",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(f"claude CLI timed out after {CLI_TIMEOUT_SECONDS}s.", file=sys.stderr)
        return 1
    except ClaudeApiError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1
    except ClaudeCliError as e:
        print(f"claude CLI error: {e}", file=sys.stderr)
        return 1

    print_report(triage)
    _append_history(log_path, triage)

    return 0


if __name__ == "__main__":
    sys.exit(main())
