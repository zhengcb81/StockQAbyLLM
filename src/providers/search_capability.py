"""Q02: one capability boundary for the four search pipeline stages.

Playbook §4 — transfer, search execution, evidence and final answer are checked
separately; HTTP 200 never proves a search happened and a tool result never
proves a final answer exists. Every verdict is a bounded reason code so callers
map it onto an existing state instead of inventing one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, NamedTuple

__all__ = [
    "STAGES",
    "Verdict",
    "search_cache_key",
    "verify_evidence_binding",
    "verify_external_retrieval",
    "verify_final_answer",
    "verify_native_search_receipt",
]

STAGES = ("transfer", "search_execution", "evidence", "final_answer")


class Verdict(NamedTuple):
    """``(ok, reason)`` — reason is a stable, bounded code."""

    ok: bool
    reason: str


def search_cache_key(
    *,
    route_id: str,
    query: str,
    locale: str,
    filters: Mapping[str, Any],
    top_k: int,
    depth: int,
    adapter_version: str,
    valid_until: str | None,
) -> str:
    """Search-result cache key covering every material retrieval input.

    TTL, locale, filters, adapter/schema version and result-count parameters
    are all part of the key, so a change in any of them invalidates the entry
    instead of silently reusing stale evidence.
    """
    material = {
        "route_id": route_id,
        "query": query,
        "locale": locale,
        "filters": dict(filters),
        "top_k": top_k,
        "depth": depth,
        "adapter_version": adapter_version,
        "valid_until": valid_until,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "SCACHE_" + hashlib.sha256(encoded).hexdigest()


def _source_urls(call: Any) -> list[str]:
    if not isinstance(call, Mapping):
        return []
    action = call.get("action")
    if not isinstance(action, Mapping):
        return []
    urls: list[str] = []
    for source in action.get("sources") or []:
        if isinstance(source, Mapping):
            url = source.get("url")
            if isinstance(url, str) and url.strip():
                urls.append(url)
    if not urls:
        for source in action.get("sources") or []:
            if isinstance(source, str) and source.strip():
                urls.append(source)
    return urls


def verify_native_search_receipt(response: Any) -> Verdict:
    """Native search proof from the answer model's own protocol.

    A 200 response, an HTTP request id, a model that SAYS it searched or a
    hand-written URL in the answer text are all insufficient (LLM-02).
    """
    if not isinstance(response, Mapping):
        return Verdict(False, "response_missing")
    if (
        response.get("http_status_code") != 200
        and type(response.get("http_status_code")) is not int
    ):
        return Verdict(False, "transfer_failed")
    calls = response.get("web_search_calls")
    if not isinstance(calls, list) or not calls:
        return Verdict(False, "search_not_executed")
    verified: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        if call.get("status") != "completed":
            continue
        if not isinstance(call.get("id"), str) or not call["id"]:
            continue
        urls = _source_urls(call)
        if not urls:
            continue
        verified.extend(urls)
    if not verified:
        return Verdict(False, "search_unverified")
    if response.get("search_status") not in (None, "executed"):
        return Verdict(False, "search_status_conflict")
    return Verdict(True, "native_search_verified")


def verify_external_retrieval(response: Any, *, expected_request_id: str | None) -> Verdict:
    """External retrieval proof: a real request id plus parsed result entries.

    An external HTTP 200 without parseable entries is NOT a search receipt,
    and an external receipt must never be presented as a native tool event.
    """
    if not isinstance(response, Mapping):
        return Verdict(False, "response_missing")
    if response.get("origin") != "external":
        return Verdict(False, "origin_not_external")
    if expected_request_id is not None and response.get("request_id") != expected_request_id:
        return Verdict(False, "request_id_mismatch")
    entries = response.get("entries")
    if not isinstance(entries, list):
        return Verdict(False, "entries_missing")
    if response.get("parse_status") == "parse_failure":
        return Verdict(False, "parse_failure")
    if not entries:
        return Verdict(False, "empty_result")
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("url"), str):
            return Verdict(False, "entry_malformed")
    return Verdict(True, "external_retrieval_verified")


def verify_evidence_binding(
    entries: Iterable[Mapping[str, Any]],
    *,
    entity_id: str,
    question_ids: Iterable[str],
    as_of: str | None,
) -> Verdict:
    """Evidence must match the entity, the question set and the cut-off date."""
    entity_seen = False
    question_bound = False
    question_set = set(question_ids)
    for entry in entries:
        if entry.get("entity_id") == entity_id:
            entity_seen = True
        mapped = entry.get("question_ids")
        if (
            entry.get("entity_id") == entity_id
            and isinstance(mapped, (list, tuple))
            and question_set & set(mapped)
        ):
            question_bound = True
        published_at = entry.get("published_at")
        if as_of is not None and isinstance(published_at, str) and published_at > as_of:
            return Verdict(False, "evidence_after_as_of")
    if not entity_seen:
        return Verdict(False, "entity_mismatch")
    if not question_bound:
        return Verdict(False, "question_not_bound")
    return Verdict(True, "evidence_bound")


def verify_final_answer(response: Any, *, question_id: str) -> Verdict:
    """A final answer exists, matches the question, and is not tool-use only."""
    if not isinstance(response, Mapping):
        return Verdict(False, "response_missing")
    if response.get("stop_reason") in {"tool_use", "max_tokens"}:
        return Verdict(False, "incomplete_no_final_answer")
    if response.get("finish_reason") == "length":
        return Verdict(False, "truncated_response")
    text = response.get("final_text")
    if not isinstance(text, str) or not text.strip():
        return Verdict(False, "final_answer_missing")
    reported = response.get("question_id")
    if reported is not None and reported != question_id:
        return Verdict(False, "question_id_mismatch")
    return Verdict(True, "final_answer_present")
