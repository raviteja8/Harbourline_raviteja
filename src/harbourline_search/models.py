from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

type DocType = Literal["policy", "runbook", "faq"]
type ErrorCode = Literal[
    "not_found",
    "method_not_allowed",
    "unauthorized",
    "unsupported_media_type",
    "invalid_request",
    "not_implemented",
    "embedding_unavailable",
    "internal",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class SearchRequest(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    top_k: Annotated[int, Field(ge=1, le=20)] = 5
    doc_type: DocType | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if value.isspace():
            raise ValueError("Query must contain a non-whitespace character.")
        return value

    @field_validator("doc_type", mode="before")
    @classmethod
    def reject_null_doc_type(cls, value: object) -> object:
        if value is None:
            raise ValueError("doc_type must be omitted rather than null.")
        return value


class SearchResult(StrictModel):
    doc_id: str
    revision: int
    chunk_id: str
    title: str
    section: str
    score: float
    snippet: Annotated[str, Field(max_length=500)]

    @field_validator("score", mode="before")
    @classmethod
    def validate_score(cls, value: object) -> float:
        if not isinstance(value, float):
            raise ValueError("Score must be a finite float without coercion.")
        return value


class SearchResponse(StrictModel):
    results: list[SearchResult]
    degraded: bool


class Citation(StrictModel):
    doc_id: str
    revision: int
    chunk_id: str
    quote: Annotated[str, Field(min_length=1, max_length=500)]


class AnswerResponse(SearchResponse):
    answer: str | None
    citations: list[Citation]
    reason: Literal["no_evidence", "model_unavailable", "invalid_model_output"] | None


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool

    @model_validator(mode="after")
    def validate_retryable(self) -> Self:
        if self.retryable != (self.code == "embedding_unavailable"):
            raise ValueError("Only embedding_unavailable is retryable.")
        return self


class ErrorEnvelope(StrictModel):
    error: ErrorDetail
