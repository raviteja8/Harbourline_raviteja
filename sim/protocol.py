import json
import re
from typing import Any

from .vectors import MODEL, embed, tokens

ANSWER_MODEL = "harbour-answer-1"

FAULT_DESCRIPTIONS = {
    "rate_limit": "First eligible attempts numbered 1, 4, 7, ... return 429; Retry-After: 1.",
    "slow": "First eligible attempts numbered 2, 5, 8, ... wait 1 second.",
    "bad_response": "First eligible attempts numbered 3, 6, 9, ... return rotating invalid shapes.",
    "outage": "Every otherwise-valid embedding attempt returns 503; Retry-After: 2.",
    "llm_outage": "Every otherwise-valid answer attempt returns 503; Retry-After: 2.",
    "llm_bad_output": "Every answer attempt returns rotating invalid output, including retries.",
}
METHODS = {
    "/": ("GET",),
    "/v1/embeddings": ("POST",),
    "/v1/chat/completions": ("POST",),
    "/sim/faults": ("GET", "POST"),
    "/sim/reset": ("POST",),
    "/sim/stats": ("GET",),
    "/sim/watch": ("GET", "POST"),
}
MESSAGES = {
    "invalid_request": "Invalid request.",
    "batch_too_large": "Batch exceeds 32 inputs.",
    "input_too_long": "Input exceeds 4,000 characters.",
    "empty_input": "Inputs must not be empty strings.",
    "unauthorized": "Unauthorized.",
    "rate_limited": "Rate limited.",
    "unavailable": "Service unavailable.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
    "internal": "Internal error.",
}


class InvalidRequest(ValueError):
    pass


def error(code: str, message: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message or MESSAGES[code]}}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise InvalidRequest()
        result[name] = value
    return result


def _constant(value: str) -> Any:
    raise InvalidRequest()


def decode(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRequest() from exc


def validate_inputs(body: Any) -> list[str]:
    if not isinstance(body, dict) or set(body) != {"model", "input"}:
        raise InvalidRequest()
    if type(body["model"]) is not str or body["model"] != MODEL:
        raise InvalidRequest()
    inputs = body["input"]
    if type(inputs) is not list or not inputs:
        raise InvalidRequest()
    for text in inputs:
        if type(text) is not str:
            raise InvalidRequest()
        try:
            text.encode("utf-8")
        except UnicodeError as exc:
            raise InvalidRequest() from exc
    return inputs


def limit_error(inputs: list[str]) -> str | None:
    if len(inputs) > 32:
        return "batch_too_large"
    if any(len(text) > 4000 for text in inputs):
        return "input_too_long"
    if any(text == "" for text in inputs):
        return "empty_input"
    return None


def validate_flags(body: Any) -> set[str]:
    if not isinstance(body, dict) or set(body) != {"flags"}:
        raise InvalidRequest()
    flags = body["flags"]
    if type(flags) is not list:
        raise InvalidRequest()
    if any(type(flag) is not str or flag not in FAULT_DESCRIPTIONS for flag in flags):
        raise InvalidRequest()
    return set(flags)


def validate_watch(body: Any) -> dict[str, str]:
    if type(body) is not dict or set(body) != {"markers"} or type(body["markers"]) is not list:
        raise InvalidRequest()
    if len(body["markers"]) > 100:
        raise InvalidRequest()
    markers = {}
    for marker in body["markers"]:
        if type(marker) is not dict or set(marker) != {"id", "text"}:
            raise InvalidRequest()
        if (type(marker["id"]) is not str or not 1 <= len(marker["id"]) <= 100
                or marker["id"] in markers or type(marker["text"]) is not str
                or not 8 <= len(marker["text"]) <= 500):
            raise InvalidRequest()
        markers[marker["id"]] = marker["text"]
    return markers


def embeddings(inputs: list[str], bad_variant: int | None = None) -> dict[str, Any]:
    data = [
        {"index": index, "embedding": embed(inputs[index])}
        for index in reversed(range(len(inputs)))
    ]
    if bad_variant == 0:
        data.pop()
    elif bad_variant == 1:
        if len(data) == 1:
            data.append({"index": data[0]["index"], "embedding": list(data[0]["embedding"])})
        else:
            data[0]["index"] = data[1]["index"]
    elif bad_variant == 2:
        data[0]["embedding"].pop()
    elif bad_variant == 3:
        data[0]["embedding"][0] = None
    return {"model": MODEL, "dimensions": 512, "data": data}


def validate_chat(body: Any) -> tuple[str, list[dict[str, str]]]:
    if type(body) is not dict or set(body) != {"model", "messages"}:
        raise InvalidRequest()
    if body["model"] != ANSWER_MODEL:
        raise InvalidRequest()
    messages = body["messages"]
    if type(messages) is not list or len(messages) != 2:
        raise InvalidRequest()
    contents = []
    for message, role in zip(messages, ("system", "user"), strict=True):
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise InvalidRequest()
        if message["role"] != role or type(message["content"]) is not str:
            raise InvalidRequest()
        try:
            message["content"].encode("utf-8")
        except UnicodeError as exc:
            raise InvalidRequest() from exc
        contents.append(message["content"])
    if sum(map(len, contents)) > 6000:
        raise InvalidRequest()
    supplied = decode(contents[1].encode("utf-8"))
    if type(supplied) is not dict or set(supplied) != {"question", "sources"}:
        raise InvalidRequest()
    if type(supplied["question"]) is not str or type(supplied["sources"]) is not list:
        raise InvalidRequest()
    try:
        supplied["question"].encode("utf-8")
    except UnicodeError as exc:
        raise InvalidRequest() from exc
    seen = set()
    for source in supplied["sources"]:
        if type(source) is not dict or set(source) != {"id", "text"}:
            raise InvalidRequest()
        if type(source["id"]) is not str or not source["id"] or source["id"] in seen:
            raise InvalidRequest()
        if type(source["text"]) is not str:
            raise InvalidRequest()
        try:
            source["id"].encode("utf-8")
            source["text"].encode("utf-8")
        except UnicodeError as exc:
            raise InvalidRequest() from exc
        seen.add(source["id"])
    return supplied["question"], supplied["sources"]


def completion(question: str, sources: list[dict[str, str]], variant: int | None = None) -> dict:
    question_words = set(tokens(question))
    candidates = []
    for source_index, source in enumerate(sources):
        for sentence_index, sentence in enumerate(re.split(r"(?<=[.!?])\s+|\n+", source["text"])):
            quote = sentence.strip()[:500]
            overlap = len(question_words.intersection(tokens(quote)))
            if overlap:
                candidates.append((-overlap, source_index, sentence_index, source["id"], quote))
    quotes = [
        {"source_id": source_id, "quote": quote}
        for _, _, _, source_id, quote in sorted(candidates)[:3]
    ]
    if variant == 1:
        unknown = "unknown-source"
        while any(source["id"] == unknown for source in sources):
            unknown += "-unknown"
        quotes = [{"source_id": unknown, "quote": "MODEL_OUTPUT_CANARY"}]
    elif variant == 2:
        invented = "MODEL_OUTPUT_CANARY: invented quote."
        while any(invented in source["text"] for source in sources):
            invented += "!"
        quotes = [{"source_id": sources[0]["id"] if sources else "s1", "quote": invented}]
    content = json.dumps({"quotes": quotes}, ensure_ascii=False, separators=(",", ":"))
    if variant == 0:
        content = "{MODEL_OUTPUT_CANARY malformed JSON"
    return {"choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": content},
        "finish_reason": "length" if variant == 3 else "stop",
    }]}
