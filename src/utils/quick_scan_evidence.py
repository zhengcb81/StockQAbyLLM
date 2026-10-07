"""Q02 evidence normalisation: frozen-query-plan results -> bounded context.

External retrieval output is untrusted DATA. Entries are deduplicated by URL,
filtered against the entity and the information cut-off date, capped at 500
Unicode characters each and 30,000 characters per company per run, and bound to
the questions named by the frozen query plan. Snippet text is never executed,
interpreted or extended — a command inside a snippet is carried verbatim as
data, and evidence that cannot support a claim is kept out of the context.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "COMPANY_EVIDENCE_LIMIT",
    "SNIPPET_LIMIT",
    "build_evidence_package",
    "select_question_context",
]

SNIPPET_LIMIT = 500
COMPANY_EVIDENCE_LIMIT = 30_000
EVIDENCE_SCHEMA = "stockqa.evidence_package/1.0.0"


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def build_evidence_package(
    candidates: Iterable[Mapping[str, Any]],
    *,
    entity_id: str,
    as_of: str | None,
    retrieved_at: str,
    adapter_version: str,
    request_id: str,
    query_bindings: Mapping[str, Sequence[str]],
    question_ids: Sequence[str],
    snippet_limit: int = SNIPPET_LIMIT,
    company_limit: int = COMPANY_EVIDENCE_LIMIT,
) -> dict[str, Any]:
    """Normalise parsed candidates into the versioned evidence package."""
    if snippet_limit < 1 or snippet_limit > SNIPPET_LIMIT:
        raise ValueError("snippet_limit must be within 1..500")
    if company_limit < 1 or company_limit > COMPANY_EVIDENCE_LIMIT:
        raise ValueError("company_limit must be within 1..30000")
    cut_off = _as_date(as_of)
    known_questions = set(question_ids)

    entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    dropped_late = 0
    dropped_entity = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        url = candidate.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        if url in seen_urls:
            continue
        candidate_entity = candidate.get("entity_id")
        if candidate_entity is not None and candidate_entity != entity_id:
            dropped_entity += 1
            continue
        published_at = candidate.get("published_at")
        published_date = _as_date(published_at)
        if cut_off is not None and published_date is not None and published_date > cut_off:
            dropped_late += 1
            continue
        seen_urls.add(url)
        query = candidate.get("query")
        bound = list(query_bindings.get(query, ())) if isinstance(query, str) else []
        bound = [question_id for question_id in bound if question_id in known_questions]
        snippet = candidate.get("snippet")
        snippet = snippet if isinstance(snippet, str) else ""
        title = candidate.get("title")
        publisher = candidate.get("publisher")
        entries.append(
            {
                "source_id": "src_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                "title": _truncate(title, snippet_limit) if isinstance(title, str) else "",
                "publisher": publisher if isinstance(publisher, str) else None,
                "url": url,
                "published_at": published_at if published_date is not None else None,
                "retrieved_at": retrieved_at,
                "short_snippet": _truncate(snippet, snippet_limit),
                "entity_id": entity_id,
                "question_ids": bound,
                # undated material cannot be shown to have been knowable at
                # the cut-off date, so it never supports a claim
                "eligible": bool(bound) and published_date is not None,
                "adapter_version": adapter_version,
                "request_id": request_id,
                "query": query if isinstance(query, str) else None,
            }
        )

    question_context, used_chars, truncated = select_question_context(
        entries, question_ids=question_ids, company_limit=company_limit
    )
    package = {
        "schema": EVIDENCE_SCHEMA,
        "entity_id": entity_id,
        "as_of": as_of,
        "retrieved_at": retrieved_at,
        "adapter_version": adapter_version,
        "request_id": request_id,
        "entries": entries,
        "question_context": question_context,
        "stats": {
            "entry_count": len(entries),
            "dropped_after_as_of": dropped_late,
            "dropped_entity_mismatch": dropped_entity,
            "context_chars": used_chars,
            "truncated": truncated,
        },
    }
    package["evidence_sha256"] = hashlib.sha256(
        _canonical(package["entries"]).encode("utf-8")
    ).hexdigest()
    return package


def select_question_context(
    entries: Sequence[Mapping[str, Any]],
    *,
    question_ids: Sequence[str],
    company_limit: int,
) -> tuple[dict[str, dict[str, Any]], int, bool]:
    """Pick each question's short evidence under one shared company budget."""
    context: dict[str, dict[str, Any]] = {}
    used = 0
    truncated = False
    for question_id in question_ids:
        chosen: list[dict[str, Any]] = []
        chars = 0
        for entry in entries:
            if question_id not in entry.get("question_ids", ()):
                continue
            if not entry.get("eligible"):
                continue
            snippet = str(entry.get("short_snippet") or "")
            cost = len(snippet)
            if used + cost > company_limit:
                truncated = True
                break
            chosen.append(
                {
                    "source_id": entry["source_id"],
                    "url": entry["url"],
                    "snippet": snippet,
                }
            )
            used += cost
            chars += cost
        context[question_id] = {"sources": chosen, "chars": chars}
    return context, used, truncated


def _canonical(value: Any) -> str:
    import json

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
