"""REST API: drug access by state, state PDLs, class landscapes, changes, the review queue,
extraction from an uploaded PDF, and the Access Copilot.

Run: ``uvicorn pdlwatch.api:app``. Keys: ``PDLWATCH_API_KEYS="acme:KEY1,beta:KEY2"`` gives each
customer its own key (``PDLWATCH_API_TOKEN`` alone still works, as one key named "default").
When any is set, every request must send one in the ``X-API-Key`` header, and the access log
(logger ``pdlwatch.access``, one JSON line per request) names the customer whose key it was,
never the key, so usage can be counted and billed per customer. Set keys before exposing the
API, e.g. to a Copilot Studio connector.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from pdlwatch import __version__
from pdlwatch.agent import tools
from pdlwatch.agent.core import Agent
from pdlwatch.agent.provider import get_provider

MAX_UPLOAD_MB = 20
MAX_UPLOAD_PAGES = 60  # OCR costs seconds per page


access_log = logging.getLogger("pdlwatch.access")


def api_keys() -> dict[str, str]:
    """Customer name -> key, from PDLWATCH_API_KEYS ("acme:KEY1,beta:KEY2") and the single
    PDLWATCH_API_TOKEN (named "default")."""
    keys: dict[str, str] = {}
    for part in os.environ.get("PDLWATCH_API_KEYS", "").split(","):
        name, sep, key = part.partition(":")
        if sep and name.strip() and key.strip():
            keys[name.strip()] = key.strip()
    token = os.environ.get("PDLWATCH_API_TOKEN")
    if token:
        keys.setdefault("default", token)
    return keys


def check_token(request: Request, x_api_key: str = Header(default="")) -> None:
    """The customer a request comes from, by its key; every key is compared in full, so the
    response time says nothing about which one nearly matched. No keys set: open (local use)."""
    keys = api_keys()
    if not keys:
        request.state.customer = "open"
        return
    given = x_api_key.encode()
    found = [name for name, key in keys.items() if secrets.compare_digest(given, key.encode())]
    if not found:
        raise HTTPException(401, "missing or wrong X-API-Key")
    request.state.customer = found[0]


app = FastAPI(
    title="PDLwatch",
    version=__version__,
    description="Medicaid preferred drug lists extracted from PDFs: drug access by state, class "
    "landscapes, quarterly changes, a review queue and an access analytics agent.",
    dependencies=[Depends(check_token)],
)


@app.middleware("http")
async def log_access(request: Request,
                     call_next: Callable[[Request], Awaitable[Response]]) -> Response:  # fmt: skip
    t0 = time.perf_counter()
    response = await call_next(request)
    access_log.info(json.dumps({
        "customer": getattr(request.state, "customer", None), "method": request.method,
        "path": request.url.path, "status": response.status_code,
        "ms": round((time.perf_counter() - t0) * 1000),
    }))  # fmt: skip
    return response


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    tools_used: list[str]
    provider: str
    latency_ms: int


def _call(tool: str, **args: Any) -> dict[str, Any]:
    out, is_error = tools.execute(tool, {k: v for k, v in args.items() if v is not None})
    payload = json.loads(out)
    if is_error:
        msg = payload["error"]
        status = 404 if ("unknown" in msg or "no drug" in msg or "no accepted rows" in msg) else 400
        raise HTTPException(status, msg)
    return dict(payload)


@app.get("/health")
def health() -> dict[str, Any]:
    from pdlwatch.pipeline import load_results

    res = load_results()
    return {"status": "ok", "data": res["data"], "results_generated_at": res["generated_at"]}


@app.get("/drugs/{drug}")
def drug_access(drug: str, as_of: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
                ) -> dict[str, Any]:  # fmt: skip
    return _call("drug_access", drug=drug, as_of=as_of)


@app.get("/states/{state}")
def state_formulary(state: str, drug_class: str | None = None) -> dict[str, Any]:
    return _call("state_formulary", state=state, drug_class=drug_class)


@app.get("/classes/{drug_class}")
def class_landscape(drug_class: str) -> dict[str, Any]:
    return _call("class_landscape", drug_class=drug_class)


@app.get("/changes")
def changes(state: str | None = None, drug: str | None = None,
            change: str | None = Query(None, pattern="^(status|added|removed|ql)$"),
            ) -> dict[str, Any]:  # fmt: skip
    return _call("pdl_changes", state=state, drug=drug, change=change)


@app.get("/review")
def review(state: str | None = None, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return _call("review_queue", state=state, limit=limit)


@app.get("/documents")
def documents(state: str | None = None) -> dict[str, Any]:
    return _call("documents", state=state)


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    return _call("extraction_quality")


@app.get("/queries")
def list_queries() -> dict[str, Any]:
    return _call("list_saved_queries")


@app.get("/queries/{name}")
def run_query(name: str) -> dict[str, Any]:
    return _call("run_saved_query", name=name)


@app.post("/extract")
async def extract(file: UploadFile, state: str | None = None) -> dict[str, Any]:
    """Extract rows from one uploaded PDL or PA criteria PDF (not stored)."""
    from pdlwatch.extract.document import extract_document

    data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF larger than {MAX_UPLOAD_MB} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "not a PDF")
    code = tools.resolve_state(state) if state else None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.pdf"
        path.write_bytes(data)
        import pdfplumber

        try:
            with pdfplumber.open(str(path)) as pdf:
                pages = len(pdf.pages)
        except Exception as e:
            raise HTTPException(422, f"unreadable PDF: {type(e).__name__}") from e
        if pages > MAX_UPLOAD_PAGES:
            raise HTTPException(413, f"{pages} pages; the limit is {MAX_UPLOAD_PAGES}")
        name = Path(file.filename or "upload").stem[:60]
        res = extract_document(path, doc_id=name, state=code)
    return {"document": {**res.summary(), "path": None},
            "rows": [r.to_dict() for r in res.rows]}  # fmt: skip


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    # the provider comes from server settings (PDLWATCH_PROVIDER), never from the caller
    r = Agent(get_provider()).ask(req.question)
    return AskResponse(
        answer=r.answer, tools_used=r.tools_used, provider=r.provider, latency_ms=r.latency_ms
    )
