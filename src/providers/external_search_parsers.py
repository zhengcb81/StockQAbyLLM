"""Offline parsers for the four external retrieval routes (Q02).

All four routes return different shapes; every one is reduced to the same
bounded candidate record. Nested JSON is unwrapped with a hard layer cap (3)
and a response byte cap, business/protocol errors are classified explicitly,
and a body that cannot be decoded is a ``parse_failure`` — never a fabricated
"zero results". Nothing here executes content found in a snippet.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

__all__ = [
    "DEFAULT_MAX_IN_MEMORY_BYTES",
    "DEFAULT_MAX_UNWRAP_LAYERS",
    "ParsedCandidates",
    "parse_brave",
    "parse_route",
    "parse_tavily",
    "parse_zai_mcp_streamable",
    "parse_zai_rest",
    "unwrap_json_text",
]

DEFAULT_MAX_UNWRAP_LAYERS = 3
DEFAULT_MAX_IN_MEMORY_BYTES = 1_000_000


class ParsedCandidates(dict):
    """``status`` is ``ok`` / ``empty`` / ``business_error`` / ``parse_failure``."""


def _bounded_text(value: Any, *, max_bytes: int) -> str:
    if isinstance(value, bytes):
        if len(value) > max_bytes:
            raise ValueError("response_exceeds_in_memory_cap")
        return value.decode("utf-8")
    if not isinstance(value, str):
        raise ValueError("response_is_not_text")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("response_exceeds_in_memory_cap")
    return value


def unwrap_json_text(
    value: Any,
    *,
    max_layers: int = DEFAULT_MAX_UNWRAP_LAYERS,
    max_bytes: int = DEFAULT_MAX_IN_MEMORY_BYTES,
) -> Any:
    """Decode a response body, unwrapping JSON-in-JSON strings up to a cap."""
    if max_layers < 1 or max_layers > 3:
        raise ValueError("max_layers must be between 1 and 3")
    current: Any = value
    for layer in range(max_layers + 1):
        if isinstance(current, (dict, list)):
            return current
        text = _bounded_text(current, max_bytes=max_bytes)
        try:
            current = json.loads(text)
        except ValueError as error:
            if layer == 0:
                raise ValueError("response_is_not_json") from error
            raise ValueError("nested_json_exceeds_layer_cap") from error
    raise ValueError("nested_json_exceeds_layer_cap")


def _result(status: str, **extra: Any) -> ParsedCandidates:
    return ParsedCandidates({"status": status, "entries": [], **extra})


def _candidate(
    *,
    title: Any,
    url: Any,
    snippet: Any,
    published_at: Any = None,
    publisher: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url.strip():
        return None
    if not url.startswith(("http://", "https://")):
        return None
    clean_title = title if isinstance(title, str) else ""
    clean_snippet = snippet if isinstance(snippet, str) else ""
    if not clean_title.strip() and not clean_snippet.strip():
        return None
    return {
        "title": clean_title.strip(),
        "url": url.strip(),
        "snippet": clean_snippet.strip(),
        "published_at": published_at if isinstance(published_at, str) and published_at else None,
        "publisher": (
            publisher.strip() if isinstance(publisher, str) and publisher.strip() else None
        ),
    }


def _finalize(candidates: list[dict[str, Any]], *, route: str) -> ParsedCandidates:
    if not candidates:
        return _result("empty", route=route)
    return ParsedCandidates({"status": "ok", "entries": candidates, "route": route})


def parse_brave(payload: Any, **limits: Any) -> ParsedCandidates:
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError as error:
        return _result("parse_failure", route="brave", error=str(error))
    if not isinstance(document, Mapping):
        return _result("parse_failure", route="brave", error="not_an_object")
    if "error" in document or (document.get("type") == "error"):
        return _result("business_error", route="brave")
    web = document.get("web")
    results = web.get("results") if isinstance(web, Mapping) else None
    if not isinstance(results, list):
        if isinstance(document.get("results"), list):
            results = document["results"]
        else:
            return _result("parse_failure", route="brave", error="missing_results_key")
    entries = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        profile = item.get("profile")
        candidate = _candidate(
            title=item.get("title"),
            url=item.get("url"),
            snippet=item.get("description") or item.get("snippet"),
            published_at=item.get("page_age") or item.get("age"),
            publisher=profile.get("name") if isinstance(profile, Mapping) else None,
        )
        if candidate is not None:
            entries.append(candidate)
    return _finalize(entries, route="brave")


def parse_tavily(payload: Any, **limits: Any) -> ParsedCandidates:
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError as error:
        return _result("parse_failure", route="tavily", error=str(error))
    if not isinstance(document, Mapping):
        return _result("parse_failure", route="tavily", error="not_an_object")
    if "detail" in document or "error" in document:
        return _result("business_error", route="tavily")
    results = document.get("results")
    if not isinstance(results, list):
        return _result("parse_failure", route="tavily", error="missing_results_key")
    entries = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        candidate = _candidate(
            title=item.get("title"),
            url=item.get("url"),
            snippet=item.get("content"),
            published_at=item.get("published_date"),
            publisher=item.get("site"),
        )
        if candidate is not None:
            entries.append(candidate)
    return _finalize(entries, route="tavily")


def _zai_entries(document: Mapping) -> list[Mapping[str, Any]] | None:
    data = document.get("data")
    for candidate in (
        data.get("results") if isinstance(data, Mapping) else None,
        data if isinstance(data, list) else None,
        document.get("results"),
        document.get("entries"),
    ):
        if isinstance(candidate, list):
            return candidate
    return None


def parse_zai_rest(payload: Any, **limits: Any) -> ParsedCandidates:
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError as error:
        return _result("parse_failure", route="zai_rest", error=str(error))
    if not isinstance(document, Mapping):
        return _result("parse_failure", route="zai_rest", error="not_an_object")
    code = document.get("code")
    if (type(code) is int and code != 0) or document.get("error") or document.get("msg"):
        return _result(
            "business_error",
            route="zai_rest",
            error_code=str(code) if code is not None else None,
        )
    results = _zai_entries(document)
    if results is None:
        return _result("parse_failure", route="zai_rest", error="missing_entries_key")
    entries = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        candidate = _candidate(
            title=item.get("title"),
            url=item.get("link") or item.get("url"),
            snippet=item.get("content") or item.get("snippet"),
            published_at=item.get("publish_date") or item.get("published_date"),
            publisher=item.get("media") or item.get("site"),
        )
        if candidate is not None:
            entries.append(candidate)
    return _finalize(entries, route="zai_rest")


def parse_zai_mcp_streamable(payload: Any, **limits: Any) -> ParsedCandidates:
    """Streamable HTTP MCP ``tools/call`` result (negotiated 2024-11-05).

    The observed tool text is a JSON string wrapping a JSON array, so the
    unwrap cap is what turns an over-nested body into a bounded parse failure.
    """
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError as error:
        return _result("parse_failure", route="zai_mcp_streamable", error=str(error))
    if not isinstance(document, Mapping):
        return _result("parse_failure", route="zai_mcp_streamable", error="not_an_object")
    if document.get("isError") is True:
        return _result("business_error", route="zai_mcp_streamable")
    if "error" in document and isinstance(document["error"], Mapping):
        return _result("business_error", route="zai_mcp_streamable")
    result = document.get("result")
    content = result.get("content") if isinstance(result, Mapping) else None
    if not isinstance(content, list):
        return _result("parse_failure", route="zai_mcp_streamable", error="missing_content_key")
    texts = [
        block.get("text")
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    if not texts:
        return _result("empty", route="zai_mcp_streamable")
    entries: list[dict[str, Any]] = []
    for text in texts:
        try:
            decoded = unwrap_json_text(text, **limits)
        except ValueError:
            return _result("parse_failure", route="zai_mcp_streamable", error="tool_text_not_json")
        items = decoded if isinstance(decoded, list) else [decoded]
        for item in items:
            if not isinstance(item, Mapping):
                continue
            candidate = _candidate(
                title=item.get("title"),
                url=item.get("url") or item.get("link"),
                snippet=item.get("summary") or item.get("content") or item.get("description"),
                published_at=item.get("published_date") or item.get("publish_date"),
                publisher=item.get("publisher") or item.get("site"),
            )
            if candidate is not None:
                entries.append(candidate)
    return _finalize(entries, route="zai_mcp_streamable")


PARSERS = {
    "brave": parse_brave,
    "tavily": parse_tavily,
    "zai_rest": parse_zai_rest,
    "zai_mcp_streamable": parse_zai_mcp_streamable,
}


def parse_route(route_kind: str, payload: Any, **limits: Any) -> ParsedCandidates:
    parser = PARSERS.get(route_kind)
    if parser is None:
        return _result("business_error", route=route_kind, error="unknown_route_kind")
    return parser(payload, **limits)
