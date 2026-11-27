# Local model simulator v1.1

Run `uv --no-config run --frozen --no-sync sim` from the repository root in
macOS/Linux or PowerShell. Python 3.14, standard library only. It binds to
`127.0.0.1`; `SIM_PORT` defaults to 8090, `SIM_API_KEY` to `test-embed-key`, and
`SIM_FAULTS` to an empty comma-separated flag list. Invalid settings or bind
failure exit 2 with a fixed diagnostic. Ctrl-C/SIGTERM closes the server.

## Wires

Both model POST routes require one bearer Authorization header. Scheme matching
is case-insensitive; keys are exact. They reject extra fields, coercion and
oversized inputs with 400, and bad keys with 401. Authentication precedes schema
checks, except the bounded transport-size check. No streaming or provider extras.
All string limits count Unicode code points, not bytes.

`POST /v1/embeddings` takes exactly:

```json
{"model":"harbour-embed-1","input":["refunds","settlement"]}
```

There must be 1-32 strings, each non-empty and at most 4,000 characters. A valid
reply has model `harbour-embed-1`, `dimensions: 512`, and `data` records with
integer `index` and 512 finite-number `embedding` coordinates. Records arrive in
reversed index order, including duplicate input strings. Match by index, not
position. Invalid limit codes are `batch_too_large`, `input_too_long`, `empty_input`.

`POST /v1/chat/completions` takes exactly:

```json
{"model":"harbour-answer-1","messages":[{"role":"system","content":"Quote only supplied evidence."},{"role":"user","content":"{\"question\":\"When do refunds expire?\",\"sources\":[{\"id\":\"s1\",\"text\":\"Refunds expire after 30 days.\"}]}"}]}
```

One system message then one user message; both contain only `role` and string
`content`. Across the two content strings the maximum is 6,000 characters,
including the JSON syntax inside user content. User JSON has exactly `question`
(string) and `sources` (list). Each source has only a unique non-empty string
`id` and string `text`. Extra fields at any level are invalid. Empty sources
are allowed. No prompt password or special system wording is needed.

Normal 200:

```json
{"choices":[{"index":0,"message":{"role":"assistant","content":"{\"quotes\":[{\"source_id\":\"s1\",\"quote\":\"Refunds expire after 30 days.\"}]}"},"finish_reason":"stop"}]}
```

The fake reads only those supplied sources, not files, hidden questions, global
IDs or previous calls. It selects up to three sentences sharing question tokens,
ordered by overlap then source/sentence order, with exact quotes capped at 500
characters. No overlap gives `{"quotes":[]}`. Instructions embedded in a document
have no execution privileges. Provenance is not entailment or proof of resistance
to prompt injection in a real model.

## Faults

| Flag | Effect | Identical retry |
| --- | --- | --- |
| `rate_limit` | Eligible keys 1, 4, 7...: 429, Retry-After 1. | Normal. |
| `slow` | Eligible keys 2, 5, 8...: one-second delay. | Normal. |
| `bad_response` | Eligible keys 3, 6, 9...: rotating malformed vectors/indexes. | Normal. |
| `outage` | All otherwise-valid embedding requests: 503, Retry-After 2. | Still fails. |
| `llm_outage` | All otherwise-valid answer requests: 503, Retry-After 2. | Still fails. |
| `llm_bad_output` | All otherwise-valid answer requests: invalid output. | Still invalid; variant changes. |

Embedding keys hash the exact input array with compact, Unicode-preserving JSON
and SHA-256. Order/case/whitespace matter. Distinct eligible keys receive slots
even when their slot's flag is disabled; no substitute fault is selected.
Outage wins over transient faults and does not consume slots. Repeated identical
arrays never repeat a transient fault until reset or a changed flag set.

Worked request keys, with no BOM or trailing newline in the hashed bytes:

```text
["hello","world"]
b'["hello","world"]'
5b2268656c6c6f222c22776f726c64225d
94bedb26fb1cb9547b5b77902e89522f313c7f7fe2e9f0175cfb0a244878ee07

["caf\u00e9","\u65e5\u672c"]
b'["caf\xc3\xa9","\xe6\x97\xa5\xe6\x9c\xac"]'
5b22636166c3a9222c22e697a5e69cac225d
fd5f81689b843abb27cf59c989c211b84495c44a1dfc963594807d3d939802fb
```

Bad embeddings cycle missing item, duplicate index, 511-coordinate vector, null
coordinate. For a singleton the duplicate variant appends a second item. Bad
answers cycle malformed content JSON, unknown source ID, invented quote, and
`finish_reason: length`. Answer outage wins over bad-output mode. These two flags
do not alter embedding behavior. A client's batching need not trigger every fault.

## Controls and fixture

Controls are unauthenticated, local and test-only. Application code must not use
them. `GET /` lists routes and flags. `GET /sim/faults` reads the sorted active
set. `POST /sim/faults` with `{"flags":["llm_bad_output"]}` replaces it; use an empty
list to clear it. Setting a different set resets key/variant numbering, not
statistics. Setting the same set is a no-op.

`POST /sim/reset` takes `{}` or an empty body. It clears counts, request records
and rotations, but keeps flags. Reset while requests are idle for a clean boundary.
The candidate README has curl and PowerShell recipes.

`GET /sim/stats` has `requests_total`, `by_status`, `inputs_total`, `max_batch_size`,
`max_in_flight`, `auth_failures`, `faults_injected`, `request_log`,
`restricted_context_requests` and `restricted_context_matches`. Both model routes
count requests/statuses; input-array counts apply only to embeddings. Records
contain arrival/response times, route, status, Retry-After seconds, an
embedding-array hash or null, and matched marker IDs, never text.
Controls/routing errors are not model attempts. Active handlers abandoned by
reset remain in the in-flight gauge but no longer update reset statistics.

`POST /sim/watch` takes `{"markers":[{"id":"restricted","text":"FIN-ORCHID"}]}`.
It observes supplied chat content only and never changes model output. GET returns
marker IDs, not text. The evaluator enables unique fixture phrases only around
denied requests, clears them before authorised controls and reports counter deltas.
Reset clears counters but preserves the watch; `{"markers":[]}` disables it.
Matches need human confirmation; absence of a match proves nothing, and identical
authorised/restricted bodies require code review. No corpus lookup occurs in the
model. Controls, including this watch, are test-only and must not be used by the app.

Use the public pytest `simulator` fixture or the `sim.testing.running_simulator`
context manager. Both reserve a free port by binding the server to port zero.
The optional `sleeper` argument supports fast timeout/fault tests. Example:

```python
from sim.testing import running_simulator

with running_simulator() as simulator:
    simulator.flags("rate_limit", "slow", "bad_response")
    base_url = simulator.url
    simulator.reset()
    simulator.flags()
```

## Transport and vectors

Responses are UTF-8 JSON with byte-accurate Content-Length and Connection: close.
Errors use `{"error":{"code":"...","message":"..."}}`, never request text. Only
429/503 carry Retry-After. Routes are exact; query strings are ignored, unknown
paths return 404 and unsupported methods return 405 with Allow. Content-Type is
not checked by this stdlib model server; the application API does check it.

JSON POSTs require one decimal Content-Length and strict UTF-8 JSON: no BOM,
duplicate keys, non-finite constants or lone surrogates. Transfer-Encoding is
rejected. Bodies up to 4 MiB are drained, including before auth/routing errors.
Larger bodies get a 400 response then a bounded drain of at most 64 MiB/10 seconds.
Reads time out after 10 seconds. Parse-level HTTP errors use the safe 400 envelope.
No access/request/body/key logging is enabled.

Embedding tokenization lowercases, extracts `[a-z0-9]+` and removes these words:

```text
a an and are as at be been being by for from in is it of on or that the this to was were with
```

Each token hashes to one of 512 signed coordinates; counts are L2-normalised.
No tokens/cancellation yields `[1.0, 0.0, ...]`. The precise implementation remains
in [vectors.py](vectors.py) for transparency, not for copying into the application.
There is no semantic/synonym model or hidden dictionary. Retry/time budgets and
response validation belong to the candidate under the contract's safety ceilings.
