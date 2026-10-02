#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端。

负责与 LLM API 进行底层通信。
"""

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple, cast
from urllib.parse import urlsplit, urlunsplit

from src.config.settings import DEFAULT_TIMEOUT, MAX_TOKENS, TEMPERATURE
from src.utils.http_client import http_client_manager
from src.utils.logger import get_logger
from src.utils.quick_scan_work_transport import begin_quick_scan_send

logger = get_logger(__name__)


class SearchCapabilityUnavailable(RuntimeError):
    """Raised before transport when the configured endpoint cannot execute web search."""


class SearchHTTPStatusError(RuntimeError):
    """A non-2xx HTTP response cannot establish a completed search."""


class LLMTransportAttemptError(RuntimeError):
    """A sanitized transport failure that retains the request attempt receipt."""

    def __init__(self, failure_type: str, attempt_receipt: Dict[str, Any]):
        self.failure_type = failure_type
        self.attempt_receipt = dict(attempt_receipt)
        super().__init__(f"LLM search request failed ({failure_type})")


@dataclass(frozen=True)
class LLMSearchResponse:
    """Text plus the minimal provider metadata needed to verify a web-search execution."""

    content: str
    request_id: Optional[str]
    response_id: Optional[str]
    actual_model: Optional[str]
    source_urls: Tuple[str, ...]
    execution_metadata: Dict[str, Any]

    @property
    def search_verified(self) -> bool:
        return self.execution_metadata.get("search_status") == "executed"


_CANONICAL_PROVIDER_NAMES = frozenset(
    {"openai", "minimax", "deepseek", "mimo", "anthropic", "glm", "kimi", "doubao"}
)
_MIMO_WEB_SEARCH_MODELS = frozenset(
    {"mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"}
)


def _search_endpoint(
    base_url: str, model: str, configured_provider: Optional[str] = None
) -> Tuple[str, str, str]:
    """Resolve one allowlisted search protocol without guessing from provider labels."""
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SearchCapabilityUnavailable("web_search requires a valid API URL") from exc
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SearchCapabilityUnavailable("web_search requires an allowlisted HTTPS API URL")

    if parsed.hostname == "api.openai.com":
        if parsed.path not in {"/v1/chat/completions", "/v1/responses"}:
            raise SearchCapabilityUnavailable(
                "configured OpenAI URL has no supported Responses API path"
            )
        endpoint, provider, protocol = (
            urlunsplit((parsed.scheme, parsed.netloc, "/v1/responses", "", "")),
            "openai",
            "responses",
        )
    elif parsed.hostname in {"api.minimaxi.com", "api.minimax.io"}:
        if model != "MiniMax-M3":
            raise SearchCapabilityUnavailable(
                "MiniMax web_search is allowlisted only for MiniMax-M3"
            )
        if parsed.path == "/v1/responses":
            protocol = "responses"
        elif (
            parsed.hostname in {"api.minimaxi.com", "api.minimax.io"}
            and parsed.path == "/anthropic/v1/messages"
        ):
            protocol = "anthropic_messages"
        else:
            raise SearchCapabilityUnavailable("MiniMax web_search URL path is not supported")
        endpoint, provider = (
            urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            "minimax",
        )
    elif parsed.hostname in {"api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"}:
        if model not in _MIMO_WEB_SEARCH_MODELS:
            raise SearchCapabilityUnavailable(
                "MiMo web_search is allowlisted only for supported v2.6 models"
            )
        if parsed.path not in {"/v1", "/v1/chat/completions"}:
            raise SearchCapabilityUnavailable("MiMo web_search URL path is not supported")
        endpoint, provider, protocol = (
            urlunsplit((parsed.scheme, parsed.netloc, "/v1/chat/completions", "", "")),
            "mimo",
            "mimo_chat_completions",
        )
    else:
        raise SearchCapabilityUnavailable("web_search is unavailable on this endpoint")

    canonical_name = (
        configured_provider.casefold() if isinstance(configured_provider, str) else None
    )
    if canonical_name in _CANONICAL_PROVIDER_NAMES and canonical_name != provider:
        raise SearchCapabilityUnavailable("configured provider does not match the search API host")
    return endpoint, provider, protocol


def _response_request_id(response: Any) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("x-request-id") or headers.get("X-Request-Id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _response_status_code(response: Any) -> Optional[int]:
    status_code = getattr(response, "status_code", None)
    return status_code if type(status_code) is int else None


def _require_successful_search_status(response: Any) -> None:
    status_code = _response_status_code(response)
    if status_code is None or not 200 <= status_code < 300:
        raise SearchHTTPStatusError("search API response did not return HTTP 2xx")


def _response_provider_error_code(response: Any) -> Optional[str]:
    """Return only known classification codes, never provider-supplied error prose."""
    try:
        payload = response.json()
    except Exception:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        for field in ("code", "type"):
            code = error.get(field)
            if isinstance(code, str):
                normalized = code.strip().lower()
                if normalized in {
                    "insufficient_quota",
                    "quota_exceeded",
                    "billing_hard_limit_reached",
                    "account_quota_exceeded",
                    "insufficient_funds",
                    "rate_limit_exceeded",
                }:
                    return normalized
    return None


def _response_retry_after_seconds(response: Any) -> Optional[float]:
    """Parse Retry-After as delta seconds or HTTP date; do not persist the raw header."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_search_response(
    response: Any, *, provider: str, requested_model: str
) -> LLMSearchResponse:
    """Bind completed search events and source URLs to one provider response."""
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Responses API returned a non-object response")

    response_id = payload.get("id")
    response_id = response_id if isinstance(response_id, str) and response_id.strip() else None
    actual_model = payload.get("model")
    actual_model = actual_model if isinstance(actual_model, str) and actual_model.strip() else None
    output = payload.get("output", [])
    if not isinstance(output, list):
        output = []

    text_parts = []
    post_search_text_parts = []
    search_calls = []
    annotation_urls = []
    active_search_call = None
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action")
            action = action if isinstance(action, dict) else {}
            urls = _extract_source_urls(action.get("sources"))
            call: Dict[str, Any] = {
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "status": item.get("status") if isinstance(item.get("status"), str) else None,
                "action_type": (
                    action.get("type") if isinstance(action.get("type"), str) else None
                ),
                "source_urls": urls,
            }
            search_calls.append(call)
            if (
                isinstance(item.get("id"), str)
                and item["id"].strip()
                and item.get("status") == "completed"
                and action.get("type") == "search"
            ):
                active_search_call = call
            else:
                active_search_call = None
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
                if active_search_call is not None and item.get("status") in (
                    None,
                    "completed",
                ):
                    post_search_text_parts.append(part["text"])
            annotations = part.get("annotations", [])
            if isinstance(annotations, list):
                urls = _extract_source_urls(annotations)
                annotation_urls.extend(urls)
                if (
                    provider == "minimax"
                    and active_search_call is not None
                    and item.get("status") in (None, "completed")
                ):
                    active_search_call["source_urls"] = list(
                        dict.fromkeys(active_search_call["source_urls"] + urls)
                    )

    request_id = _response_request_id(response)
    response_status = payload.get("status") if isinstance(payload.get("status"), str) else None
    if not search_calls:
        observed_search_calls = 0
    elif all(call["action_type"] == "search" for call in search_calls):
        observed_search_calls = len(search_calls)
    else:
        # A web_search_call item with an unknown action cannot safely be billed
        # as zero unless the provider supplied an explicit usage total.
        observed_search_calls = None
    verified_calls = [
        call
        for call in search_calls
        if call["id"]
        and call["status"] == "completed"
        and call["action_type"] == "search"
        and call["source_urls"]
    ]
    verified_call_urls = [url for call in verified_calls for url in call["source_urls"]]
    usage = _normalize_provider_usage(
        payload.get("usage"),
        protocol="responses",
        observed_search_calls=observed_search_calls,
    )
    if provider == "minimax":
        # MiniMax's documented Responses example places URLs in the final message
        # after web_search_call, while action.sources may be absent.
        source_urls = tuple(dict.fromkeys(verified_call_urls))
        content = "\n".join(post_search_text_parts).strip()
        search_verified = bool(
            response_status == "completed"
            and response_id
            and actual_model == requested_model
            and verified_calls
            and source_urls
            and content
        )
    else:
        source_urls = tuple(dict.fromkeys(verified_call_urls + annotation_urls))
        content = "\n".join(text_parts).strip()
        search_verified = bool(
            response_status == "completed" and request_id and response_id and verified_calls
        )
    execution_metadata = {
        "provider": provider,
        "search_status": "executed" if search_verified else "unverified",
        "request_id": request_id,
        "response_id": response_id,
        "response_status": response_status,
        "search_receipt_id": verified_calls[0]["id"] if search_verified else None,
        "actual_model": actual_model,
        "web_search_calls": search_calls,
        "source_urls": list(source_urls),
    }
    if usage is not None:
        execution_metadata["usage"] = usage
    return LLMSearchResponse(
        content=content,
        request_id=request_id,
        response_id=response_id,
        actual_model=actual_model,
        source_urls=source_urls,
        execution_metadata=execution_metadata,
    )


_ANTHROPIC_SEARCH_ERROR_CODES = frozenset(
    {
        "too_many_requests",
        "invalid_tool_input",
        "max_uses_exceeded",
        "query_too_long",
        "request_too_large",
        "unavailable",
    }
)


def _parse_anthropic_search_response(response: Any, *, requested_model: str) -> LLMSearchResponse:
    """Bind Anthropic-style server search results to their exact tool-use IDs."""
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Anthropic Messages API returned a non-object response")

    response_id = payload.get("id")
    response_id = response_id if isinstance(response_id, str) and response_id.strip() else None
    actual_model = payload.get("model")
    actual_model = actual_model if isinstance(actual_model, str) and actual_model.strip() else None
    stop_reason = (
        payload.get("stop_reason") if isinstance(payload.get("stop_reason"), str) else None
    )
    blocks = payload.get("content", [])
    if not isinstance(blocks, list):
        blocks = []

    calls = []
    by_id = {}
    duplicate_call_ids = set()
    duplicate_result_ids = set()
    unbound_result_seen = False
    unrecognized_server_tool_use_seen = False
    search_completed = False
    answer_parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "server_tool_use" and block.get("name") == "web_search":
            call_id = block.get("id") if isinstance(block.get("id"), str) else None
            call: Dict[str, Any] = {
                "id": call_id,
                "status": "pending",
                "action_type": "search",
                "source_urls": [],
            }
            calls.append(call)
            search_completed = False
            if not call_id or not call_id.strip():
                continue
            if call_id in by_id:
                duplicate_call_ids.add(call_id)
            else:
                by_id[call_id] = call
        elif block_type == "server_tool_use":
            # The request only authorizes web_search. Treat every other server-side
            # tool as an unverified response rather than silently accepting it.
            unrecognized_server_tool_use_seen = True
        elif block_type == "web_search_tool_result":
            tool_use_id = block.get("tool_use_id")
            matched_call = by_id.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if matched_call is None:
                unbound_result_seen = True
                search_completed = False
                continue
            call = matched_call
            if tool_use_id in duplicate_result_ids or call["status"] != "pending":
                duplicate_result_ids.add(tool_use_id)
                call["status"] = "failed"
                search_completed = False
                continue
            result_content = block.get("content")
            if isinstance(result_content, list):
                call["status"] = "completed"
                call["source_urls"] = _extract_source_urls(result_content)
                search_completed = True
            elif isinstance(result_content, dict):
                call["status"] = "failed"
                error_code = result_content.get("error_code")
                if isinstance(error_code, str) and error_code in _ANTHROPIC_SEARCH_ERROR_CODES:
                    call["error_code"] = error_code
                search_completed = False
            else:
                call["status"] = "failed"
                search_completed = False
        elif block_type == "text" and search_completed and isinstance(block.get("text"), str):
            answer_parts.append(block["text"])

    request_id = _response_request_id(response)
    source_urls = tuple(dict.fromkeys(url for call in calls for url in call["source_urls"]))
    content = "\n".join(answer_parts).strip()
    base_resp_present = "base_resp" in payload
    base_resp = payload.get("base_resp")
    base_resp_status = base_resp.get("status_code") if isinstance(base_resp, dict) else None
    base_resp_ok = not base_resp_present or (
        isinstance(base_resp, dict) and type(base_resp_status) is int and base_resp_status == 0
    )
    search_verified = bool(
        response_id
        and actual_model == requested_model
        and stop_reason == "end_turn"
        and base_resp_ok
        and len(calls) == 1
        and calls[0]["id"]
        and calls[0]["id"] not in duplicate_call_ids
        and calls[0]["status"] == "completed"
        and not duplicate_result_ids
        and not unbound_result_seen
        and not unrecognized_server_tool_use_seen
        and source_urls
        and content
    )
    metadata = {
        "provider": "minimax",
        "search_status": "executed" if search_verified else "unverified",
        "request_id": request_id,
        "response_id": response_id,
        "response_status": (
            "completed" if stop_reason == "end_turn" and base_resp_ok else stop_reason
        ),
        "stop_reason": stop_reason,
        "search_receipt_id": calls[0]["id"] if search_verified else None,
        "actual_model": actual_model,
        "web_search_calls": calls,
        "source_urls": list(source_urls),
    }
    usage = _normalize_provider_usage(
        payload.get("usage"),
        protocol="anthropic_messages",
        observed_search_calls=len(calls),
    )
    if usage is not None:
        metadata["usage"] = usage
    return LLMSearchResponse(
        content=content,
        request_id=request_id,
        response_id=response_id,
        actual_model=actual_model,
        source_urls=source_urls,
        execution_metadata=metadata,
    )


def _parse_mimo_search_response(response: Any, *, requested_model: str) -> LLMSearchResponse:
    """Normalize MiMo Chat Completions citations as response-bound search evidence."""
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MiMo Chat Completions returned a non-object response")

    response_id = payload.get("id")
    response_id = (
        response_id.strip() if isinstance(response_id, str) and response_id.strip() else None
    )
    actual_model = payload.get("model")
    actual_model = (
        actual_model.strip() if isinstance(actual_model, str) and actual_model.strip() else None
    )
    choices = payload.get("choices")
    choice = (
        choices[0]
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict)
        else {}
    )
    raw_message = choice.get("message")
    message: Dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    raw_content = message.get("content")
    content: str = raw_content if isinstance(raw_content, str) else ""
    finish_reason = (
        choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
    )
    annotations = message.get("annotations")
    citation_annotations = [
        item
        for item in annotations or []
        if isinstance(annotations, list)
        and isinstance(item, dict)
        and item.get("type") == "url_citation"
    ]
    source_urls = tuple(_extract_source_urls(citation_annotations))
    request_id = _response_request_id(response)
    verified = bool(
        response_id
        and actual_model == requested_model
        and finish_reason == "stop"
        and content.strip()
        and citation_annotations
        and source_urls
    )
    # MiMo returns response-level citations, not a separate search-call ID. Use the
    # response ID as the correlation key for this one response-bound citation set.
    search_calls = (
        [
            {
                "id": response_id,
                "status": "completed" if verified else "unverified",
                "action_type": "search",
                "source_urls": list(source_urls),
                "evidence_basis": "url_citation_annotations",
            }
        ]
        if citation_annotations
        else []
    )
    metadata = {
        "provider": "mimo",
        "search_status": "executed" if verified else "unverified",
        "request_id": request_id,
        "response_id": response_id,
        "response_status": "completed" if finish_reason == "stop" else finish_reason,
        "search_receipt_id": response_id if verified else None,
        "actual_model": actual_model,
        "web_search_calls": search_calls,
        "source_urls": list(source_urls),
    }
    usage = _normalize_provider_usage(
        payload.get("usage"), protocol="chat_completions", observed_search_calls=None
    )
    if usage is not None:
        metadata["usage"] = usage
    return LLMSearchResponse(
        content=content,
        request_id=request_id,
        response_id=response_id,
        actual_model=actual_model,
        source_urls=source_urls,
        execution_metadata=metadata,
    )


def _parse_protocol_search_response(
    response: Any, *, provider: str, protocol: str, requested_model: str
) -> LLMSearchResponse:
    if protocol == "anthropic_messages":
        return _parse_anthropic_search_response(response, requested_model=requested_model)
    if protocol == "mimo_chat_completions":
        return _parse_mimo_search_response(response, requested_model=requested_model)
    return _parse_search_response(response, provider=provider, requested_model=requested_model)


def _usage_count(value: Any) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _nested_usage_count(usage: Dict[str, Any], *path: str) -> Optional[int]:
    current: Any = usage
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _usage_count(current)


def _first_usage_count(usage: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key in usage:
            return _usage_count(usage[key])
    return None


def _normalize_provider_usage(
    value: Any, *, protocol: str, observed_search_calls: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Keep only validated billable usage counters from the provider response.

    The normalized input count excludes cached input; cached and cache-creation
    tokens remain separate so a rate-card resolver can price each category.
    Search calls must be provider-reported or be represented by actual tool-use
    items in this same response. Missing or malformed counters remain unknown.
    """
    if not isinstance(value, dict):
        return None

    if protocol == "anthropic_messages":
        input_tokens = _first_usage_count(value, "input_tokens")
        cached_input_tokens = _first_usage_count(value, "cache_read_input_tokens")
        cache_creation_input_tokens = _first_usage_count(value, "cache_creation_input_tokens")
        output_tokens = _first_usage_count(value, "output_tokens")
        reasoning_output_tokens = _first_usage_count(value, "reasoning_tokens")
        if any(
            key in value and count is None
            for key, count in (
                ("cache_read_input_tokens", cached_input_tokens),
                ("cache_creation_input_tokens", cache_creation_input_tokens),
                ("reasoning_tokens", reasoning_output_tokens),
            )
        ):
            return None
        if reasoning_output_tokens is None:
            reasoning_output_tokens = _nested_usage_count(
                value, "output_tokens_details", "reasoning_tokens"
            )
            details = value.get("output_tokens_details")
            if (
                isinstance(details, dict)
                and "reasoning_tokens" in details
                and reasoning_output_tokens is None
            ):
                return None
    else:
        total_input_tokens = _first_usage_count(value, "input_tokens", "prompt_tokens")
        output_tokens = _first_usage_count(value, "output_tokens", "completion_tokens")
        if total_input_tokens is None:
            return None
        cached_input_tokens = (
            _nested_usage_count(value, "input_tokens_details", "cached_tokens")
            if "input_tokens" in value
            else _nested_usage_count(value, "prompt_tokens_details", "cached_tokens")
        )
        # Some OpenAI-compatible responses omit detail objects when the count is
        # zero. Treat absence as zero only after the provider supplied total usage.
        if cached_input_tokens is None:
            details_key = (
                "input_tokens_details" if "input_tokens" in value else "prompt_tokens_details"
            )
            details = value.get(details_key)
            if details is None:
                cached_input_tokens = 0
            elif isinstance(details, dict) and "cached_tokens" not in details:
                cached_input_tokens = 0
        cache_creation_input_tokens = 0
        reasoning_output_tokens = (
            _nested_usage_count(value, "output_tokens_details", "reasoning_tokens")
            if "output_tokens" in value
            else _nested_usage_count(value, "completion_tokens_details", "reasoning_tokens")
        )
        if reasoning_output_tokens is None:
            details_key = (
                "output_tokens_details" if "output_tokens" in value else "completion_tokens_details"
            )
            details = value.get(details_key)
            if details is None or (isinstance(details, dict) and "reasoning_tokens" not in details):
                reasoning_output_tokens = 0
            elif isinstance(details, dict):
                return None
        if cached_input_tokens is None or cached_input_tokens > total_input_tokens:
            return None
        input_tokens = total_input_tokens - cached_input_tokens

    if protocol == "anthropic_messages":
        if cached_input_tokens is None:
            cached_input_tokens = 0
        if cache_creation_input_tokens is None:
            cache_creation_input_tokens = 0
    if input_tokens is None or output_tokens is None:
        return None
    if cached_input_tokens is None or cache_creation_input_tokens is None:
        return None
    if reasoning_output_tokens is None:
        reasoning_output_tokens = 0
    if any(
        count < 0
        for count in (
            input_tokens,
            cached_input_tokens,
            cache_creation_input_tokens,
            output_tokens,
            reasoning_output_tokens,
        )
    ):
        return None
    if reasoning_output_tokens > output_tokens:
        return None

    reported_search_usage = value.get("web_search_usage")
    reported_search_calls = (
        _usage_count(reported_search_usage.get("tool_usage"))
        if isinstance(reported_search_usage, dict) and "tool_usage" in reported_search_usage
        else None
    )
    if isinstance(reported_search_usage, dict) and "tool_usage" in reported_search_usage:
        search_tool_calls = reported_search_calls
        if (
            search_tool_calls is not None
            and observed_search_calls is not None
            and search_tool_calls < observed_search_calls
        ):
            return None
    else:
        search_tool_calls = _usage_count(observed_search_calls)
    if search_tool_calls is None:
        return None

    return {
        "schema": "quick-scan-usage-v1",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "search_tool_calls": search_tool_calls,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _with_attempt_receipt(
    parsed: LLMSearchResponse,
    prompt: str,
    attempt_id: str,
    started_at: str,
    response: Any,
) -> LLMSearchResponse:
    metadata = dict(parsed.execution_metadata)
    metadata.update(
        {
            "http_status_code": _response_status_code(response),
            "attempt_id": attempt_id,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
    )
    metadata["attempts"] = [
        {
            key: metadata[key]
            for key in (
                "provider",
                "request_id",
                "response_id",
                "actual_model",
                "search_status",
                "response_status",
                "http_status_code",
                "started_at",
                "completed_at",
                "attempt_id",
                "prompt_sha256",
                "search_receipt_id",
                "source_urls",
                "web_search_calls",
                "usage",
            )
            if key in metadata
        }
    ]
    return LLMSearchResponse(
        content=parsed.content,
        request_id=parsed.request_id,
        response_id=parsed.response_id,
        actual_model=parsed.actual_model,
        source_urls=parsed.source_urls,
        execution_metadata=metadata,
    )


def _failed_attempt_receipt(
    response: Any,
    attempt_id: str,
    started_at: str,
    prompt: str,
    error: Exception,
    *,
    provider: str,
) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {
        "provider": provider,
        "request_id": _response_request_id(response),
        "response_id": None,
        "actual_model": None,
        "search_status": "unverified",
        "response_status": None,
        "http_status_code": _response_status_code(response),
        "started_at": started_at,
        "completed_at": _utc_now(),
        "attempt_id": attempt_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "search_receipt_id": None,
        "source_urls": [],
        "web_search_calls": [],
        "failure_type": type(error).__name__,
    }
    provider_error_code = _response_provider_error_code(response)
    retry_after_seconds = _response_retry_after_seconds(response)
    if provider_error_code is not None:
        receipt["provider_error_code"] = provider_error_code
    if retry_after_seconds is not None:
        receipt["retry_after_seconds"] = retry_after_seconds
    return receipt


def _extract_source_urls(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    urls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if (
            isinstance(url, str)
            and urlsplit(url).scheme in {"http", "https"}
            and urlsplit(url).netloc
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))


def _search_request_headers(api_key: str, protocol: str) -> Dict[str, str]:
    if protocol == "anthropic_messages":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _search_request_payload(
    prompt: str, system_prompt: str, model: str, provider: str, protocol: str
) -> Dict[str, Any]:
    if protocol == "anthropic_messages":
        return {
            "model": model,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            # Official Messages API ToolChoice supports only auto/none; forcing a
            # named tool is not part of the contract (Q02 official-API conformance).
            "tool_choice": {"type": "auto"},
        }
    if protocol == "mimo_chat_completions":
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {
                    "type": "web_search",
                    "max_keyword": 2,
                    "force_search": True,
                    "limit": 3,
                }
            ],
            "tool_choice": "auto",
            "stream": False,
            "max_completion_tokens": 2048,
            "thinking": {"type": "disabled"},
        }
    if provider == "minimax":
        # Server Tools Responses shape plus the official `instructions` field for
        # system text (Q02). Merging the Chinese system line into `input` empirically
        # suppressed web_search invocation (3/3 zero-search live+probe runs vs 4/4
        # searching without it); keep input purely English task text.
        return {
            "model": model,
            "instructions": system_prompt,
            "input": prompt,
            "tools": [{"type": "web_search"}],
        }
    return {
        "model": model,
        "instructions": system_prompt,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "max_output_tokens": MAX_TOKENS,
        "store": False,
    }


class LLMClient:
    """同步 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        provider_name: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.provider_name = provider_name

    @property
    def supports_web_search(self) -> bool:
        try:
            _search_endpoint(self.base_url, self.model, self.provider_name)
            return True
        except SearchCapabilityUnavailable:
            return False

    def send_request(
        self,
        prompt: str,
        system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。",
    ) -> str:
        """发送同步请求。"""
        session = http_client_manager.get_sync_session()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        logger.debug("发送请求到 %s (模型: %s)", self.base_url, self.model)

        response = session.post(self.base_url, headers=headers, json=data, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()

        content = cast(str, result["choices"][0]["message"]["content"])
        return content

    def send_search_request(
        self,
        prompt: str,
        system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。",
    ) -> LLMSearchResponse:
        """Call an allowlisted Responses web_search route and preserve its receipt."""
        endpoint, provider, protocol = _search_endpoint(
            self.base_url, self.model, self.provider_name
        )
        session = http_client_manager.get_sync_session()
        headers = _search_request_headers(self.api_key, protocol)
        data = _search_request_payload(prompt, system_prompt, self.model, provider, protocol)
        attempt_id = str(uuid.uuid4())
        started_at = _utc_now()
        work_attempt = begin_quick_scan_send(prompt, system_prompt)
        if work_attempt is not None:
            work_attempt.consume_for_post()
        response = None
        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=data,
                timeout=self.timeout,
                allow_redirects=False,
            )
            response.raise_for_status()
            _require_successful_search_status(response)
            parsed = _parse_protocol_search_response(
                response,
                provider=provider,
                protocol=protocol,
                requested_model=self.model,
            )
        except Exception as error:
            receipt = _failed_attempt_receipt(
                response, attempt_id, started_at, prompt, error, provider=provider
            )
            if work_attempt is not None:
                work_attempt.record_failure(receipt)
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        result = _with_attempt_receipt(parsed, prompt, attempt_id, started_at, response)
        if work_attempt is not None:
            work_attempt.record_response(
                http_status_code=_response_status_code(response),
                request_id=result.request_id,
                receipt=result.execution_metadata,
            )
        return result


class AsyncLLMClient:
    """异步 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        provider_name: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.provider_name = provider_name

    @property
    def supports_web_search(self) -> bool:
        try:
            _search_endpoint(self.base_url, self.model, self.provider_name)
            return True
        except SearchCapabilityUnavailable:
            return False

    async def send_request_async(
        self,
        prompt: str,
        system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。",
    ) -> str:
        """发送异步请求。"""
        client = await http_client_manager.get_async_client()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        logger.debug("发送异步请求到 %s (模型: %s)", self.base_url, self.model)

        response = await client.post(
            self.base_url,
            headers=headers,
            json=data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()

        content = cast(str, result["choices"][0]["message"]["content"])
        return content

    async def send_search_request_async(
        self,
        prompt: str,
        system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。",
    ) -> LLMSearchResponse:
        """Async counterpart of the same allowlisted search adapter."""
        endpoint, provider, protocol = _search_endpoint(
            self.base_url, self.model, self.provider_name
        )
        client = await http_client_manager.get_async_client()
        headers = _search_request_headers(self.api_key, protocol)
        data = _search_request_payload(prompt, system_prompt, self.model, provider, protocol)
        attempt_id = str(uuid.uuid4())
        started_at = _utc_now()
        work_attempt = begin_quick_scan_send(prompt, system_prompt)
        if work_attempt is not None:
            work_attempt.consume_for_post()
        response = None
        try:
            response = await client.post(
                endpoint,
                headers=headers,
                json=data,
                timeout=self.timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            _require_successful_search_status(response)
            parsed = _parse_protocol_search_response(
                response,
                provider=provider,
                protocol=protocol,
                requested_model=self.model,
            )
        except Exception as error:
            receipt = _failed_attempt_receipt(
                response, attempt_id, started_at, prompt, error, provider=provider
            )
            if work_attempt is not None:
                work_attempt.record_failure(receipt)
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        result = _with_attempt_receipt(parsed, prompt, attempt_id, started_at, response)
        if work_attempt is not None:
            work_attempt.record_response(
                http_status_code=_response_status_code(response),
                request_id=result.request_id,
                receipt=result.execution_metadata,
            )
        return result
