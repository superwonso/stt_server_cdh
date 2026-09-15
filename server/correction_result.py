"""Canonical, source-safe persistence of completed correction answers."""
from __future__ import annotations
from .llm_result import RESULT_WARNING, WARNING_CODES, draft_document, safe_draft_text
from .postprocessor import PostprocessingError


def completed_correction_result(output, sources):
    incoming = getattr(output, "segments", None)
    terms = getattr(output, "uncertain_terms", None)
    draft = getattr(output, "draft_text", None)
    warnings = getattr(output, "warnings", ())
    flagged = bool(warnings)
    if not isinstance(terms, list) or len(terms) > 1000 or any(not isinstance(t, str) or len(t) > 200 for t in terms):
        terms, flagged = [], True
    clean_terms = []
    for term in terms:
        if not term.strip() or term.strip() == RESULT_WARNING:
            continue
        try:
            safe_term = safe_draft_text(term, max_chars=200)
            flagged = flagged or safe_term != term.strip()
            clean_terms.append(safe_term)
        except ValueError:
            flagged = True
    terms = clean_terms
    clean = []
    try:
        if draft is not None:
            raise ValueError("Detached answer")
        if (not isinstance(incoming, list) or any(not isinstance(item, dict) for item in incoming)
                or [item.get("id") for item in incoming] != [item["id"] for item in sources]):
            raise ValueError("Unverified mapping")
        for source, item in zip(sources, incoming, strict=True):
            text = item.get("text")
            if (not isinstance(text, str) or not text.strip()
                    or len(text.strip()) > max(1000, len(source["text"]) * 4 + 500)):
                raise ValueError("Unverified body")
            safe = safe_draft_text(text)
            flagged = flagged or safe != text.strip()
            clean.append({"id": source["id"], "start": source["start"], "end": source["end"], "text": safe})
        text = "\n".join(row["text"] for row in clean)
        if not text:
            raise ValueError("No answer")
    except (TypeError, KeyError, ValueError):
        try:
            value = {"text": draft} if isinstance(draft, str) and draft.strip() else {"segments": incoming}
            document = draft_document(value)
            text, clean, flagged = document["text"], [], True
        except (TypeError, ValueError):
            raise PostprocessingError("invalid_response", "AI에서 표시할 결과 본문을 받지 못했습니다.") from None
    if flagged:
        terms.append(RESULT_WARNING)
    return {"text": text, "segments": clean, "uncertain_terms": terms,
            "warnings": ["validation_failed"] if flagged else []}
