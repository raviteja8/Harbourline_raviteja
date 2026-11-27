import hashlib
import json
import math
import re


MODEL = "harbour-embed-1"
DIMENSIONS = 512
STOP_WORDS = (
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by",
    "for", "from", "in", "is", "it", "of", "on", "or", "that", "the",
    "this", "to", "was", "were", "with",
)
_STOP_WORDS = frozenset(STOP_WORDS)
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_PATTERN.findall(text.lower()) if token not in _STOP_WORDS]


def request_key(inputs: list[str]) -> str:
    encoded = json.dumps(inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def embed(text: str) -> list[float]:
    counts = [0] * DIMENSIONS
    for token in tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        dimension = int.from_bytes(digest[:2], "big") % DIMENSIONS
        sign = -1 if digest[2] & 1 else 1
        counts[dimension] += sign
    norm = math.sqrt(sum(value * value for value in counts))
    if norm == 0:
        return [1.0] + [0.0] * (DIMENSIONS - 1)
    return [value / norm for value in counts]
