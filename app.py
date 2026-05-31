"""
extraction.py
=============
LLM-based invoice extraction, provider-agnostic. Sends a PDF to a vision model
and gets back a strict JSON structure. This is the *only* unreliable step in the
pipeline; it is deliberately isolated so the deterministic builder/validator
never depends on the model getting numbers perfectly right.

Two safety mechanisms make that isolation safe:
  1. The model extracts line items AND the invoice's printed totals
     INDEPENDENTLY. The pipeline (reconcile) then checks the lines add up to the
     printed totals. A misread digit almost always breaks that equation.
  2. All numbers are returned as STRINGS to avoid float precision loss, and the
     model is told never to round or recompute — only to copy what it sees.

Supported providers
--------------------
* "anthropic"  — Claude.   pip install anthropic        env: ANTHROPIC_API_KEY
* "gemini"     — Gemini.   pip install google-genai     env: GEMINI_API_KEY
                 (Google offers a free developer key at aistudio.google.com.)

Provider selection (see get_extractor): explicit argument > whichever key is set.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable

# Default models per provider. Override via the get_extractor() / function args.
ANTHROPIC_MODEL = "claude-opus-4-8"
GEMINI_MODEL = "gemini-2.5-flash"

EXTRACTION_PROMPT = r"""
You are an expert accounting data-extraction system. You are given an invoice as
a PDF (any layout, any language - Latvian, English, etc.). Extract its data for
conversion into a Peppol e-invoice.

Read the RENDERED document visually. The embedded text layer may be corrupt or
missing - trust what you see on the page, not the raw text.

Return ONLY a single JSON object - no markdown fences, no commentary, nothing
before or after. Use this exact schema (use null for any field not present):

{
  "invoice_id": string,
  "issue_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD" | null,
  "currency": string,
  "buyer_reference": string | null,
  "contract_reference": string | null,
  "supplier": {
    "name": string,
    "reg_number": string,
    "vat_number": string | null,
    "street": string | null,
    "city": string | null,
    "postal_zone": string | null,
    "country_code": string,
    "contact_name": string | null,
    "contact_phone": string | null,
    "contact_email": string | null
  },
  "customer": { ...same shape as supplier... },
  "delivery": {
    "street": string | null, "city": string | null,
    "postal_zone": string | null, "country_code": string | null
  } | null,
  "delivery_date": "YYYY-MM-DD" | null,
  "payment": {
    "iban": string | null,
    "bic": string | null,
    "bank_name": string | null,
    "payment_id": string | null,
    "terms_note": string | null
  } | null,
  "lines": [
    {
      "description": string,
      "seller_item_id": string | null,
      "quantity": string,
      "unit_code": string,
      "net_unit_price": string,
      "vat_category": "S" | "AE" | "Z" | "E",
      "vat_percent": string,
      "printed_line_total": string | null
    }
  ],
  "printed_totals": {
    "net_excl_vat": string | null,
    "total_vat": string | null,
    "grand_total_incl_vat": string | null
  }
}

CRITICAL RULES:
- NUMBERS: return every numeric value as a STRING, exactly as printed. Never
  round, never reformat, never recompute. Preserve all decimal places shown.
- UNIT PRICE: use the net price per unit AFTER any line discount, excluding VAT.
  If the invoice shows both a gross/list price and a discounted price, use the
  discounted net price (the amount actually charged per unit before VAT).
- printed_line_total and printed_totals are COPIED from the page. Do not derive
  them - they exist so a separate system can cross-check your line extraction.
- VAT: standard rate -> "S" with the percent. Reverse charge -> "AE", percent
  "0". Zero-rated -> "Z". Exempt -> "E". If unsure, use "S" and the percent shown.
- UNIT CODES (map the printed unit to UN/ECE): piece/gab/pcs -> "C62";
  metre/m -> "MTR"; pair/paris -> "PR"; square metre/m2 -> "MTQ"; kg -> "KGM";
  litre/l -> "LTR"; hour -> "HUR". Unknown -> "C62".
- IDENTIFIERS: copy registration and VAT numbers exactly; keep the country
  prefix on VAT numbers (e.g. "LV...").
- Never invent a value. If something is not on the page, use null.
- Capture EVERY line item, including ones that repeat across pages.
""".strip()


def _decode_json(text: str) -> dict[str, Any]:
    """Parse the model's reply into a dict, tolerating stray fences/whitespace."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip().strip("`").strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1:
            s = s[i:j + 1]
    return json.loads(s)


def extract_with_anthropic(
    pdf_bytes: bytes,
    *,
    model: str = ANTHROPIC_MODEL,
    client: Any = None,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64",
                            "media_type": "application/pdf",
                            "data": b64}},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _decode_json(text)


def extract_with_gemini(
    pdf_bytes: bytes,
    *,
    model: str = GEMINI_MODEL,
    client: Any = None,
) -> dict[str, Any]:
    if client is None:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    from google.genai import types

    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            # Large invoices produce a lot of JSON; a low cap silently truncates
            # the response and breaks JSON parsing. Give it generous headroom.
            max_output_tokens=65536,
        ),
    )
    text = resp.text
    if not text or not text.strip():
        # Empty response — usually a safety block or a truncated/empty candidate.
        reason = getattr(getattr(resp, "candidates", [None])[0], "finish_reason", None)
        raise RuntimeError(f"Gemini returned an empty response (finish_reason={reason}).")
    return _decode_json(text)


Extractor = Callable[[bytes], dict[str, Any]]


def available_providers() -> list[str]:
    """Which providers have a usable API key in the environment."""
    out = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append("anthropic")
    if os.environ.get("GEMINI_API_KEY"):
        out.append("gemini")
    return out


def get_extractor(provider: str | None = None) -> Extractor:
    """Return a bytes -> dict extractor for the chosen provider.

    provider: "anthropic" | "gemini" | None. If None, picks whichever key is set
    (Anthropic preferred when both are present). Raises if none is configured.
    """
    if provider is None:
        avail = available_providers()
        if not avail:
            raise RuntimeError(
                "No LLM API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY."
            )
        provider = avail[0]

    provider = provider.lower()
    if provider == "anthropic":
        return extract_with_anthropic
    if provider == "gemini":
        return extract_with_gemini
    raise ValueError(f"Unknown provider: {provider!r}. Use 'anthropic' or 'gemini'.")


def extract_invoice(pdf_bytes: bytes, *, provider: str | None = None) -> dict[str, Any]:
    """Backwards-compatible default: auto-selected provider."""
    return get_extractor(provider)(pdf_bytes)