"""Offline parsers for the four external retrieval routes (Q02).

All four routes return different shapes; every one is reduced to the same
bounded candidate record. Nested JSON is unwrapped with a hard layer cap (3)
and a response byte cap, business/protocol errors are classified explicitly,
and a body that cannot be decoded is a ``parse_failure`` — never a fabricated
"zero results". Nothing here executes content found in a snippet.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("nonfinite_json_number")


def strict_search_json(text: str) -> Any:
    """Reject ambiguous objects and all nonfinite numbers, including 1e400."""
    result = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    json.dumps(result, allow_nan=False)
    return result


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
            # Object input is still provider data. It must not bypass either
            # the wire-size bound or finite JSON representation requirement.
            encoded = json.dumps(current, ensure_ascii=False, allow_nan=False)
            _bounded_text(encoded, max_bytes=max_bytes)
            return current
        text = _bounded_text(current, max_bytes=max_bytes)
        try:
            current = strict_search_json(text)
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
    if any(ord(c) < 32 for c in url):
        return None
    url = url.strip()
    # The durable receipt accepts HTTPS short records only. Reject individual
    # unsafe candidates here, before a paid response reaches journal validation.
    if len(url) > 2048 or any(ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        parts.port  # malformed port syntax must not escape the response parser
    except (ValueError, UnicodeError):
        return None
    clean_title = title if isinstance(title, str) else ""
    clean_snippet = snippet if isinstance(snippet, str) else ""
    if not clean_title.strip() and not clean_snippet.strip():
        return None
    return {
        "title": clean_title.strip(),
        "url": url.strip(),
        "snippet": clean_snippet.strip(),
        "published_at": (
            published_at
            if isinstance(published_at, str)
            and published_at.strip()
            and len(published_at) <= 100
            and not any(ord(c) < 32 for c in published_at)
            else None
        ),
        "publisher": (
            publisher.strip() if isinstance(publisher, str) and publisher.strip() else None
        ),
    }


def _finalize(
    candidates: list[dict[str, Any]], *, route: str, raw_count: int | None = None
) -> ParsedCandidates:
    if not candidates:
        result = _result("empty", route=route)
    else:
        result = ParsedCandidates({"status": "ok", "entries": candidates, "route": route})
    if raw_count is not None:
        result["provider_result_count"] = raw_count
    return result


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
    return _finalize(entries, route="brave", raw_count=len(results))


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
    return _finalize(entries, route="tavily", raw_count=len(results))


def _zai_entries(document: Mapping) -> list[Mapping[str, Any]] | None:
    data = document.get("data")
    for candidate in (
        document.get("search_result"),
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
    return _finalize(entries, route="zai_rest", raw_count=len(results))


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
    if isinstance(result, Mapping) and "isError" in result:
        if type(result["isError"]) is not bool:
            return _result(
                "parse_failure", route="zai_mcp_streamable", error="invalid_tool_error_state"
            )
        if result["isError"]:
            return _result("business_error", route="zai_mcp_streamable")
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
    raw_count = 0
    for text in texts:
        try:
            decoded = unwrap_json_text(text, **limits)
        except ValueError:
            return _result("parse_failure", route="zai_mcp_streamable", error="tool_text_not_json")
        items = decoded if isinstance(decoded, list) else [decoded]
        raw_count += len(items)
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
    return _finalize(entries, route="zai_mcp_streamable", raw_count=raw_count)


def validate_fetch_url(url: Any, *, allow_document_links: bool = False) -> str:
    """Only exact public HTTPS page URLs, never local/binary/document targets."""
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 2000
        or any(ord(c) < 33 or c == "\\" for c in url)
    ):
        raise ValueError("unsafe_fetch_url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.fragment
            or "%" in parts.netloc
            or "." not in host
            or host.endswith(".")
            or host.endswith((".local", ".localhost", ".internal"))
        ):
            raise ValueError("unsafe_fetch_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:  # refuse all IP literals, including public ones
            raise ValueError("unsafe_fetch_url")
        path = unquote(parts.path).lower()
        if not allow_document_links and any(
            ext in unquote(url).lower()
            for ext in (
                ".pdf",
                ".doc",
                ".xls",
                ".ppt",
                ".zip",
                ".exe",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".mp4",
                ".bin",
            )
        ):
            raise ValueError("fetch_document_or_binary_forbidden")
        if any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("unsafe_fetch_url")
    except (ValueError, UnicodeError):
        raise ValueError("unsafe_fetch_url") from None
    return url


def parse_deepseek_search(payload: Any, **limits: Any) -> ParsedCandidates:
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError:
        return _result("parse_failure", route="deepseek_search")
    if not isinstance(document, Mapping):
        return _result("parse_failure", route="deepseek_search")
    if "error" in document:
        return _result("business_error", route="deepseek_search")
    results = document.get("results")
    if not isinstance(results, list):
        return _result("parse_failure", route="deepseek_search")
    entries = []
    for item in results:
        if not isinstance(item, Mapping):
            return _result("parse_failure", route="deepseek_search")
        try:
            validate_fetch_url(item.get("url"), allow_document_links=True)
        except ValueError:
            continue
        summary = item.get("summary")
        use_summary = isinstance(summary, str) and bool(summary.strip())
        candidate = _candidate(
            title=item.get("title"),
            url=item.get("url"),
            snippet=summary if use_summary else item.get("snippet"),
            published_at=item.get("date"),
            publisher=item.get("site_name"),
        )
        if candidate is not None:
            candidate["snippet"] = candidate["snippet"][:500]
            candidate["snippet_kind"] = "provider_summary" if use_summary else "search_snippet"
            entries.append(candidate)
    # Provider result count, not filtered count, controls reference billing.
    result = _finalize(entries, route="deepseek_search")
    result["provider_result_count"] = len(results)
    return result


def parse_deepseek_fetch(payload: Any, **limits: Any) -> ParsedCandidates:
    try:
        document = unwrap_json_text(payload, **limits)
    except ValueError:
        return _result("parse_failure", route="deepseek_fetch")
    if isinstance(document, Mapping) and "error" in document:
        return _result("business_error", route="deepseek_fetch")
    if not isinstance(document, Mapping) or not isinstance(document.get("content"), str):
        return _result("parse_failure", route="deepseek_fetch")
    content = document["content"]
    if content.startswith(("%PDF", "PK\x03\x04")) or "\x00" in content:
        return _result("parse_failure", route="deepseek_fetch")
    result = _result("ok" if content else "empty", route="deepseek_fetch")
    result.update(
        provider_result_count=int(bool(content)),
        excerpt=content[:500],
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        title=document.get("title") if isinstance(document.get("title"), str) else "",
    )
    return result


PARSERS = {
    "brave": parse_brave,
    "deepseek_search": parse_deepseek_search,
    "deepseek_fetch": parse_deepseek_fetch,
    "tavily": parse_tavily,
    "zai_rest": parse_zai_rest,
    "zai_mcp_streamable": parse_zai_mcp_streamable,
}


def parse_route(route_kind: str, payload: Any, **limits: Any) -> ParsedCandidates:
    parser = PARSERS.get(route_kind)
    if parser is None:
        return _result("business_error", route=route_kind, error="unknown_route_kind")
    return parser(payload, **limits)
