"""
app.py
======
FastAPI service: PDF in -> reviewable extraction -> confirmed JSON -> XML.

Endpoints
---------
GET  /providers  which LLM providers are configured (have an API key set).
POST /extract    multipart PDF upload (optional ?provider=anthropic|gemini).
                 Returns the extracted data (editable), computed totals, and a
                 list of flags for the review screen. Does NOT produce XML yet.
POST /generate   JSON body = the (possibly user-corrected) extraction. Re-runs
                 reconciliation + Peppol calculation validation, then returns
                 the UBL XML. Rejects with 422 if validation fails.
GET  /health     liveness check.

Run:  pip install fastapi "uvicorn[standard]" python-multipart \
                  anthropic google-genai
      export ANTHROPIC_API_KEY=...   # and/or
      export GEMINI_API_KEY=...
      uvicorn app:app --reload
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse

from extraction import get_extractor, available_providers, Extractor
from pipeline import map_to_invoice, reconcile
from peppol_horizon import build_invoice_xml, validate

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("invoice-api")

app = FastAPI(title="Invoice -> Peppol (Horizon) converter")

# Allow the React dev frontend to call this API from the browser.
# Tighten allow_origins to your real frontend origin(s) in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional override for tests: set app.state.extractor to a fake bytes->dict fn.
# When None, the provider is resolved per request from the query / env keys.
app.state.extractor = None


def _resolve_extractor(provider: Optional[str]) -> Extractor:
    if app.state.extractor is not None:
        return app.state.extractor
    try:
        return get_extractor(provider)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/providers")
async def providers() -> dict[str, Any]:
    avail = available_providers()
    return {"available": avail, "default": avail[0] if avail else None}


@app.post("/extract")
async def extract_endpoint(
    file: UploadFile = File(...),
    provider: Optional[str] = Query(None, description="anthropic | gemini"),
) -> JSONResponse:
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Please upload a PDF.")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "Empty file.")

    extractor = _resolve_extractor(provider)

    def _is_rate_or_auth(err: Exception) -> bool:
        m = str(err).lower()
        return any(t in m for t in ("429", "resource_exhausted", "rate limit", "quota",
                                    "401", "403", "permission", "api key", "unauthenticated"))

    # Retry transient extractor failures (e.g. an occasional malformed/empty
    # model response) server-side, so the user doesn't have to re-upload. Do NOT
    # retry rate-limit/auth errors — retrying those is pointless.
    ext = None
    last_err = None
    for attempt in range(3):
        try:
            ext = extractor(pdf_bytes)
            break
        except Exception as e:
            last_err = e
            logger.warning("Extraction attempt %d failed: %s", attempt + 1, e)
            if _is_rate_or_auth(e):
                break

    if ext is None:
        e = last_err
        logger.exception("Extraction failed", exc_info=e)
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "resource_exhausted" in low or "rate limit" in low or "quota" in low:
            raise HTTPException(429, f"Rate limit reached for the LLM provider: {msg}")
        if "401" in msg or "403" in msg or "permission" in low or "api key" in low or "unauthenticated" in low:
            raise HTTPException(401, f"LLM provider auth error: {msg}")
        raise HTTPException(502, f"Extraction failed: {type(e).__name__}: {msg}")

    invoice = map_to_invoice(ext)
    rec = reconcile(invoice, ext)

    return JSONResponse({
        "extraction": ext,
        "computed_totals": {
            "net": str(rec.computed_net),
            "vat": str(rec.computed_vat),
            "total": str(rec.computed_total),
        },
        "flags": [asdict(f) for f in rec.flags],
        "ok": rec.ok,
    })


@app.post("/generate")
async def generate_endpoint(payload: dict[str, Any]) -> Response:
    """payload is the confirmed/corrected extraction dict (same shape as
    /extract's "extraction"). Returns XML, or 422 with the problems."""
    invoice = map_to_invoice(payload)

    rec = reconcile(invoice, payload)
    blocking = [asdict(f) for f in rec.flags if f.level == "error"]

    xml = build_invoice_xml(invoice)
    val = validate(xml)

    if blocking or not val.ok:
        return JSONResponse(
            status_code=422,
            content={
                "reconcile_errors": blocking,
                "validation_errors": val.errors,
                "validation_warnings": val.warnings,
            },
        )

    filename = (invoice.invoice_id or "invoice").replace(" ", "_") + ".xml"
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}