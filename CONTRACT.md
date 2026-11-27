# Harbourline Payments: Take-Home Contract v1.2

## 1 The task

Build a small Python service for an internal assistant:

- First, build the core: persistent ingestion, access-controlled search, and a quote-backed answer using the supplied model simulator. Handle failures safely.
- Then check and hand over: focused tests, one visible evaluation, a short decisions note and your existing AI-session exports. These are part of the task, not optional extras.
- Leave out: UI, deployment, real authentication, model downloads, agents, general text parsing and multi-round revisions.

Start whenever you like. Plan for about 2 hours. Report active time honestly, including handover, with setup/problems separately.

Choose chunking, prefixes, ranking, storage, internal structure, retries, context selection and abstention. The same take-home applies to every candidate. More features are not expected.

We supply HTTP validation/errors, settings, text helpers, a simulator fixture and an evaluator. Setup, run, Git and export recipes are in [README.md](README.md).

## 2 Data and ingestion

All documents are in `data/round1/`. The JSONL manifest has `doc_id`, `revision`, `title`, `doc_type`, `groups`, `path`, `tombstone`. Structure/paths are trusted; IDs are unique, revision is 1 and tombstone is false. There is no round2.

`harbourline_search.manifest.load_manifest(round_dir: Path)` yields records with `.row` and `.content` bytes. `harbourline_search.text.prepare_text(content: bytes)` returns prepared `.text` and `.section_at(offset)` for a zero-based text offset, or raises `ValueError` with `invalid_encoding` or `empty_content`. The helper owns decoding, markup and heading positions; do not reimplement it.

`identities.json` lists known groups and maps fixture tokens to caller groups. Validate document groups as a non-empty list of known names. Skip invalid groups or unusable text, reporting IDs/reasons; no error precedence is assessed. Document instructions and URLs are inert: never execute them, fetch links or let them change permissions.

`ingest round1` stores valid documents in `DATA_DIR`. Deterministic chunks must cover the prepared text and retain document identity. Overlap/prefixes are your choices. Re-ingestion must not duplicate results; a service restart must retain data. Never expose half an embedded document. Preserve a previous complete index or publish only complete documents; explain your policy. Stop the service during ingestion.

Exit codes: **0** success (invalid-document skips allowed), **1** operational/document failure, **2** invalid configuration, unknown round or embedding 401; **3** is reserved for the unfinished starter. Report counts, affected IDs and reasons, not document text.

## 3 Search and access

`GET /health` returns public 200. `POST /search` and `POST /answer` require a known bearer token before retrieval or model calls; the bearer scheme is case-insensitive. A document is allowed if its groups include `public` or intersect the caller's groups. Filter before top-k and model context. Identical content never merges permissions.

Both endpoints accept only `query` (non-blank string, 1-500 characters), `top_k` (integer 1-20, default 5) and optional `doc_type` (`policy`, `runbook`, `faq`; omit rather than null). Reject extra fields and coercion, including `top_k: 5.0`. Require `application/json`; charset parameters are allowed. Metadata filters only narrow access.

Use simulator embeddings; lexical signals may supplement them. Return at most top_k distinct chunks, most relevant first, with deterministic ties:

```json
{"results":[{"doc_id":"refunds","revision":1,"chunk_id":"c1","title":"Refunds","section":"Policy","score":0.8,"snippet":"Refund requests expire after 30 days."}],"degraded":false}
```

Scores must be finite. Snippets are plain excerpts, whitespace collapsed, at most 500 characters. Use the helper's section label or `""` if absent. Do not expose forbidden content, IDs, counts or existence.

Preserve supplied safe errors: 400 `invalid_request`, 401 `unauthorized`, 415 `unsupported_media_type`, 404 `not_found`, 405 `method_not_allowed`, 500 `internal`. Query embedding failure, including provider 401, returns 503 `embedding_unavailable` on either endpoint. Only 503 is retryable; 501 `not_implemented` is the unfinished starter response. The supplied envelope is `{"error":{"code":"invalid_request","message":"Invalid request.","retryable":false}}`; never echo request data. No multi-error ordering is assessed.

## 4 Model boundary

Both local models use `EMBED_URL` and `EMBED_API_KEY`; supplied URL helpers preserve base paths. They are lexical/extractive stand-ins, not real semantic models. Do not recreate their outputs locally or hard-code answers. Character limits count Unicode code points.

### Embeddings

`POST /v1/embeddings` accepts `{"model":"harbour-embed-1","input":["text"]}` with bearer authentication: 1-32 non-empty strings, each at most 4,000 characters including prefixes. Choose your batching.

Responses have model `harbour-embed-1`, dimensions 512 and `data` items containing `index` and `embedding`. Items arrive reversed: match by integer index, not position. Require exactly one item per input and 512 finite numbers per vector; reject missing/duplicate indexes, booleans and coercion before storage.

### Answers

`POST /v1/chat/completions` accepts `model: "harbour-answer-1"` and `messages`: one system message, then one user message, each with `role` and string `content`. No streaming. Write system instructions; user content is a JSON string:

```json
{"question":"When do refund requests expire?","sources":[{"id":"s1","text":"Refund requests expire after 30 days."}]}
```

Choose/shorten authorised retrieved passages to fit **6,000 characters across both content strings**, including instructions and JSON syntax. Map unique call-local source IDs to retrieved chunks. Never put tokens in context. The model selects up to three sentences sharing question content words, or no quotes without overlap. No magic prompt wording unlocks success.

Normal HTTP 200:

```json
{"choices":[{"index":0,"message":{"role":"assistant","content":"{\"quotes\":[{\"source_id\":\"s1\",\"quote\":\"Refund requests expire after 30 days.\"}]}"},"finish_reason":"stop"}]}
```

Require one completed choice (`finish_reason: "stop"`) and valid quote JSON. Each non-empty quote (maximum 500 characters, three quotes) must occur exactly in the text sent under its source ID. That ID must map to an authorised retrieved chunk. Build citations from your mapping, never model assertions. Quotes prove provenance, not relevance or factual truth. Model output is data, never instructions to execute.

`/answer` reuses search and calls the answer model when context is usable. Join validated quotes with newlines; add trusted citations:

```json
{"results":[{"doc_id":"refunds","revision":1,"chunk_id":"c1","title":"Refunds","section":"Policy","score":0.8,"snippet":"Refund requests expire after 30 days."}],"degraded":false,"answer":"Refund requests expire after 30 days.","citations":[{"doc_id":"refunds","revision":1,"chunk_id":"c1","quote":"Refund requests expire after 30 days."}],"reason":null}
```

No context/quotes: HTTP 200, null answer, empty citations, reason `no_evidence`, degraded false. Answer-model failure: the same shape with reason `model_unavailable`, degraded true. Invalid/truncated output: reason `invalid_model_output`, degraded true. Keep authorised search results. Reject the whole invalid reply; never return raw model text. Valid answers and safe abstention both matter.

Decide what `/answer` does when allowed documents disagree, and be ready to explain your choice.

### Faults and limits

| Flag | Effect | Identical retry |
| --- | --- | --- |
| `rate_limit` | Selected embedding attempts: 429, Retry-After 1 | Does not repeat |
| `slow` | Selected embedding attempts: one-second delay | Does not repeat |
| `bad_response` | Selected embedding attempts: invalid vectors/indexes | Does not repeat |
| `outage` / `llm_outage` | Embedding / answer service: 503, Retry-After 2 | Persists |
| `llm_bad_output` | Malformed JSON, unknown citation, invented quote or truncated completion | Persists; variant may change |

For embeddings, identical means the same input array between resets/flag changes. Do not depend on scheduling or force every fault to occur. Controls are for tests only; the README supplies recipes. Both services return 400 for invalid/oversized input and 401 for bad credentials.

Choose bounded retries/timeouts; honour Retry-After or stop if it exceeds your budget. Never retry an unchanged 400 or a 401; stop ingestion model calls on 401. Review stops hangs after 10 seconds/API request, 180/ingest, 30/startup and 300/tests plus checks. These are process limits, not candidate working-time limits.

## 5 Measurement and handover

The supplied `evaluate` command starts temporary processes/storage, ingests round1 and measures eight retrieval questions: any expected document in the first five chunks is one hit; service errors are misses. Four access/answer controls are separate. It also checks re-ingestion/restart consistency and unsafe application logs. Hidden checks use the same corpus, identities and rules; no hidden documents or revision requirements.

Submit code/tests, evaluator output and a short decisions note with three items: the decision you'd most like to be asked about; one AI output you didn't trust, how you checked it, and which session file and turn shows it; and gaps and time spent. Update run instructions only if needed.

Tests should check important outcomes and failure boundaries, not maximise test count. Verification means checking a consequential claim, such as whether a denied document reaches the model or an invented citation is rejected. Explain what your check establishes; a formatter pass alone does not establish correctness.

## 6 AI evidence and submission

Use task-only AI sessions and keep full available prompts/replies, including corrections and abandoned approaches. Commit native exports under `submission/ai-session-history/`, with available plans/tool traces and task instructions used. Do not curate a highlights reel or reconstruct unavailable context; declare gaps. Faithful labelled transcripts are acceptable where export is unavailable. No provider-hidden reasoning or unrelated chats.

Mark secret/personal redactions. Use Windows-safe export filenames, such as `session-01.jsonl`, and confirm files are visible in the repo. We judge task direction, consequential checking and corrections or justified acceptance, not prompt volume or manufactured disagreements. A summary/share link alone is insufficient.

The hiring panel may read all submitted histories and use AI to review them. Reviewer-controlled copies, including extracts and AI-review sessions, are deleted **180 days after the hiring decision**. Your own repo/copies remain yours.

Commit the untouched starter first, then keep development commits without squashing. Push to your private GitHub repo and grant access to the reviewer named in your invitation. Reply with the repo URL, branch, full commit SHA and time estimate. The repo is the only submission; no separate attachment is required. Git recipes are in the README.

Keep the task, solution and histories confidential; share only with the hiring panel and your chosen AI tools for this exercise. Push/commit patterns support review but do not prove hours or authorship. Missing timing evidence or a different reported duration does not reduce marks. The no-AI follow-up is the main ownership check. For problems or unusual cases, the hiring lead decides; contact the address in your invitation.

## 7 Environment and review

Use Python 3.14, uv >=0.12.0 and fixed supplied dependencies. Keep dependency/build metadata, lockfile and supplied inputs unchanged. Keep tests/fixtures in `tests/`; code stays in the supplied package under `src/`. No extra packages, services, submodules or LFS. Application execution is offline; development AI may use the internet.

Native development supports macOS arm64, Windows x86_64 PowerShell and Linux x86_64/aarch64. Reviewers select your declared commit and run it offline in a fixed Linux environment, restoring inputs. The runner imports `src/harbourline_search`, calls `cli.ingest`/`cli.serve` and runs tests/checks using its Python, without building/installing your project. Preserve those entry points. Tests/subprocesses use that interpreter, not nested `uv run` or downloads.

Application logs/errors must not expose queries, document/model text, bodies or tokens. Use IDs, counts, timings and fixed reasons; AI histories follow section 6. **A human-confirmed access leak fails the safety requirement.**

## What we look for

We look for a correct and safe core, sensible choices you can explain, checking what matters, and ownership of your code.

After submission there is a live follow-up that includes a small live change to your code.
