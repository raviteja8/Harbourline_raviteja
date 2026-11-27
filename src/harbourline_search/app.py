from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from .models import ErrorCode, ErrorDetail, ErrorEnvelope, SearchRequest

ERRORS: dict[int, tuple[ErrorCode, str]] = {
    400: ("invalid_request", "Invalid request."),
    401: ("unauthorized", "Unauthorized."),
    404: ("not_found", "Not found."),
    405: ("method_not_allowed", "Method not allowed."),
    415: ("unsupported_media_type", "Unsupported media type."),
    500: ("internal", "Internal error."),
    501: ("not_implemented", "Not implemented."),
    503: ("embedding_unavailable", "Embedding unavailable."),
}


def error_response(status: int) -> JSONResponse:
    status = status if status in ERRORS else 500
    code, message = ERRORS[status]
    body = ErrorEnvelope(error=ErrorDetail(code=code, message=message, retryable=status == 503))
    return JSONResponse(status_code=status, content=body.model_dump())


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def safe_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and request.url.path in {"/search", "/answer"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return error_response(415)
        try:
            return await call_next(request)
        except Exception:
            return error_response(500)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(400)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(error.status_code)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    install_error_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/search")
    def search(request: SearchRequest) -> JSONResponse:
        return error_response(501)

    @app.post("/answer")
    def answer(request: SearchRequest) -> JSONResponse:
        return error_response(501)

    return app
