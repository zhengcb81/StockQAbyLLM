#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端。

负责与 LLM API 进行底层通信。
"""

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple, cast
from urllib.parse import urlsplit, urlunsplit

from src.config.settings import DEFAULT_TIMEOUT, MAX_TOKENS, TEMPERATURE
from src.providers.model_resolution import (
    model_resolution_allowed,
    model_resolution_sha256,
    normalize_model_resolution,
)
from src.utils.http_client import http_client_manager
from src.utils.logger import get_logger
from src.utils.quick_scan_work_transport import (
    attach_external_use_proof,
    begin_quick_scan_send,
    frozen_quick_scan_model_resolution,
    render_external_request,
)

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
        external = self.execution_metadata.get("external_context_use")
        if "external_context_use" not in self.execution_metadata:
            return self.execution_metadata.get("search_status") == "executed"
        from src.utils.quick_scan_work_store import quick_scan_receipt_sha256

        transport = self.execution_metadata.get("work_transport")
        if (
            not isinstance(external, dict)
            or external.get("schema")
            not in {"stockqa.external_context_use/1.0.0", "stockqa.external_context_use/1.1.0"}
            or external.get("state") != "request_and_response_bound"
            or not isinstance(transport, dict)
            or external.get("work_attempt_id") != transport.get("work_attempt_id")
            or external.get("provider") != self.execution_metadata.get("provider")
            or external.get("actual_model") != self.actual_model
            or external.get("llm_receipt_sha256")
            != quick_scan_receipt_sha256(self.execution_metadata)
        ):
            return False
        proof = {key: value for key, value in external.items() if key != "proof_sha256"}
        try:
            digest = hashlib.sha256(
                json.dumps(
                    proof,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        except (TypeError, ValueError):
            return False
        return external.get("proof_sha256") == digest and (
            external.get("answer_search_mode") == "external_context_only"
            or (
                external.get("answer_search_mode") == "native_with_external_context"
                and self.execution_metadata.get("search_status") == "executed"
            )
        )


_CANONICAL_PROVIDER_NAMES = frozenset(
    {"openai", "minimax", "deepseek", "mimo", "anthropic", "glm", "kimi", "doubao"}
)
_MIMO_WEB_SEARCH_MODELS = frozenset(
    {"mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"}
)


def _search_endpoint(
    base_url: str,
    model: str,
    configured_provider: Optional[str] = None,
    *,
    external_context_only: bool = False,
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
    elif parsed.hostname == "api.deepseek.com" and external_context_only:
        if parsed.path not in {"/responses", "/v1/responses"}:
            raise SearchCapabilityUnavailable(
                "DeepSeek external answer requires a supported Responses path"
            )
        endpoint, provider, protocol = (
            urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            "deepseek",
            "responses",
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
    response: Any,
    *,
    provider: str,
    requested_model: str,
    model_resolution: Any = None,
    response_payload: Any = None,
) -> LLMSearchResponse:
    """Bind completed search events and source URLs to one provider response."""
    payload = response.json() if response_payload is None else response_payload
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
            sources = _extract_sources(action.get("sources"))
            call: Dict[str, Any] = {
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "status": item.get("status") if isinstance(item.get("status"), str) else None,
                "action_type": (
                    action.get("type") if isinstance(action.get("type"), str) else None
                ),
                "source_urls": [entry["url"] for entry in sources],
                "sources": sources,
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
                    merged_sources = {
                        entry["url"]: entry for entry in active_search_call.get("sources", [])
                    }
                    for entry in _extract_sources(annotations):
                        merged_sources.setdefault(entry["url"], entry)
                    active_search_call["sources"] = list(merged_sources.values())

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
            and model_resolution_allowed(
                provider, "responses", requested_model, actual_model, model_resolution
            )
            and verified_calls
            and source_urls
            and content
        )
    else:
        source_urls = tuple(dict.fromkeys(verified_call_urls + annotation_urls))
        content = "\n".join(text_parts).strip()
        search_verified = bool(
            response_status == "completed"
            and request_id
            and response_id
            and verified_calls
            and model_resolution_allowed(
                provider, "responses", requested_model, actual_model, model_resolution
            )
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


def _parse_anthropic_search_response(
    response: Any,
    *,
    requested_model: str,
    model_resolution: Any = None,
    response_payload: Any = None,
) -> LLMSearchResponse:
    """Bind Anthropic-style server search results to their exact tool-use IDs."""
    payload = response.json() if response_payload is None else response_payload
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
                call["sources"] = _extract_sources(result_content)
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
        and model_resolution_allowed(
            "minimax", "anthropic_messages", requested_model, actual_model, model_resolution
        )
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


def _parse_mimo_search_response(
    response: Any,
    *,
    requested_model: str,
    model_resolution: Any = None,
    response_payload: Any = None,
) -> LLMSearchResponse:
    """Normalize MiMo Chat Completions citations as response-bound search evidence."""
    payload = response.json() if response_payload is None else response_payload
    if not isinstance(payload, dict):
        raise ValueError("MiMo Chat Completions returned a non-object response")

    response_id = payload.get("id")
    response_id = (
        response_id.strip() if isinstance(response_id, str) and response_id.strip() else None
    )
    actual_model = payload.get("model")
    actual_model = actual_model if isinstance(actual_model, str) and actual_model.strip() else None
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
        and model_resolution_allowed(
            "mimo", "mimo_chat_completions", requested_model, actual_model, model_resolution
        )
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
                "sources": _extract_sources(citation_annotations),
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
    response: Any,
    *,
    provider: str,
    protocol: str,
    requested_model: str,
    model_resolution: Any = None,
    response_payload: Any = None,
) -> LLMSearchResponse:
    if protocol == "anthropic_messages":
        return _parse_anthropic_search_response(
            response,
            requested_model=requested_model,
            model_resolution=model_resolution,
            response_payload=response_payload,
        )
    if protocol == "mimo_chat_completions":
        return _parse_mimo_search_response(
            response,
            requested_model=requested_model,
            model_resolution=model_resolution,
            response_payload=response_payload,
        )
    return _parse_search_response(
        response,
        provider=provider,
        requested_model=requested_model,
        model_resolution=model_resolution,
        response_payload=response_payload,
    )


def _parse_external_answer_response(
    response: Any,
    *,
    provider: str,
    protocol: str,
    requested_model: str,
    model_resolution: Any,
    response_payload: Any,
) -> LLMSearchResponse:
    """Retain actual protocol metadata while reading final text without native tools.

    Reasoning blocks are never final output. Unexpected tools or incomplete
    results are not accepted just because external evidence was provided.
    """
    parsed = _parse_protocol_search_response(
        response,
        provider=provider,
        protocol=protocol,
        requested_model=requested_model,
        model_resolution=model_resolution,
        response_payload=response_payload,
    )
    payload = response_payload
    if parsed.execution_metadata.get("web_search_calls"):
        raise ValueError("external-only response includes unrequested native search")
    parts = []
    if protocol == "responses":
        if payload.get("status") != "completed" or not isinstance(payload.get("output"), list):
            raise ValueError("external answer is not complete")
        for item in payload["output"]:
            if not isinstance(item, dict):
                raise ValueError("invalid external answer output")
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") not in {None, "completed"}
            ):
                raise ValueError("external-only response contains an unrequested output")
            for block in item.get("content", []):
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "output_text"
                    or not isinstance(block.get("text"), str)
                ):
                    raise ValueError("invalid external answer text")
                parts.append(block["text"])
    elif protocol == "anthropic_messages":
        if payload.get("stop_reason") != "end_turn":
            raise ValueError("external answer is not complete")
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}:
                continue
            if (
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise ValueError("external-only response contains an unrequested tool")
            parts.append(block["text"])
    else:
        choices = payload.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or choices[0].get("finish_reason") != "stop"
        ):
            raise ValueError("external answer is not complete")
        message = choices[0].get("message", {})
        if (
            message.get("tool_calls")
            or message.get("function_call")
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("external-only response contains an unrequested tool")
        parts.append(message["content"])
    content = "\n".join(parts).strip()
    if not content or not parsed.response_id or not parsed.request_id:
        raise ValueError("external answer has no complete response identity/text")
    usage = _normalize_provider_usage(
        payload.get("usage"),
        protocol="chat_completions" if protocol == "mimo_chat_completions" else protocol,
        observed_search_calls=0,
    )
    metadata = dict(parsed.execution_metadata)
    if usage is not None:
        metadata["usage"] = usage
    return replace(parsed, content=content, execution_metadata=metadata)


def _canonical_http_json(response: Any) -> Tuple[Any, str, str]:
    """Hash canonical JSON, not network bytes; real raw JSON is strictly decoded.

    Synthetic response doubles without text/content use an explicitly labelled
    parsed payload. That basis makes no claim about duplicate raw JSON keys.
    """

    def pairs(items: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate HTTP JSON key")
            result[key] = value
        return result

    raw = getattr(response, "content", None)
    if not isinstance(raw, (str, bytes)):
        raw = getattr(response, "text", None)
    if isinstance(raw, (str, bytes)):
        payload = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite HTTP JSON")),
        )
        basis = "strict_http_json"
    else:
        payload, basis = response.json(), "parsed_payload"

    def finite_json(value: Any) -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("nonfinite HTTP JSON")
            return
        if type(value) is list:
            for item in value:
                finite_json(item)
            return
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("HTTP JSON object key must be a string")
                finite_json(item)
            return
        raise ValueError("HTTP payload is not finite JSON")

    finite_json(payload)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return payload, hashlib.sha256(encoded).hexdigest(), basis


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
    *,
    requested_model: str,
    protocol: str,
    response_sha256: str,
    response_json_basis: str,
    model_resolution: Any,
) -> LLMSearchResponse:
    metadata = dict(parsed.execution_metadata)
    metadata.update(
        {
            "http_status_code": _response_status_code(response),
            "requested_model": requested_model,
            "search_protocol": protocol,
            "response_sha256": response_sha256,
            "response_json_basis": response_json_basis,
            "model_resolution_sha256": model_resolution_sha256(model_resolution),
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
                "requested_model",
                "search_protocol",
                "response_sha256",
                "response_json_basis",
                "model_resolution_sha256",
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
    protocol: Optional[str] = None,
    requested_model: Optional[str] = None,
    model_resolution: Any = None,
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
    # Only explicit provider-reported model and complete usage can price
    # a rejected request. HTTP status alone proves neither zero cost nor usage.
    if protocol is not None:
        try:
            payload, response_sha256, response_json_basis = _canonical_http_json(response)
            reported_model = payload.get("model") if isinstance(payload, dict) else None
            reported_id = payload.get("id") if isinstance(payload, dict) else None
            receipt.update(
                requested_model=requested_model,
                search_protocol=protocol,
                response_sha256=response_sha256,
                response_json_basis=response_json_basis,
                model_resolution_sha256=model_resolution_sha256(model_resolution),
            )
            status_code = receipt["http_status_code"]
            if type(status_code) is int and 300 <= status_code < 400:
                # A redirect body is not a protocol response. Keep its digest,
                # but do not promote its claimed identity or usage.
                return receipt
            if isinstance(reported_id, str) and reported_id.strip():
                receipt["response_id"] = reported_id
            if (
                isinstance(reported_model, str)
                and reported_model.strip()
                and len(reported_model) <= 160
                and not any(ord(c) < 32 for c in reported_model)
            ):
                receipt["actual_model"] = reported_model
            raw_usage = payload.get("usage") if isinstance(payload, dict) else None
            coherent = isinstance(raw_usage, dict)
            if isinstance(raw_usage, dict):
                for left, right in (
                    ("input_tokens", "prompt_tokens"),
                    ("output_tokens", "completion_tokens"),
                ):
                    if left in raw_usage and right in raw_usage:
                        coherent = (
                            coherent
                            and type(raw_usage[left]) is int
                            and type(raw_usage[right]) is int
                            and raw_usage[left] == raw_usage[right]
                        )
                for detail in (
                    "input_tokens_details",
                    "prompt_tokens_details",
                    "output_tokens_details",
                    "completion_tokens_details",
                ):
                    if detail in raw_usage and not isinstance(raw_usage[detail], dict):
                        coherent = False
            usage = (
                _normalize_provider_usage(
                    raw_usage,
                    protocol=protocol,
                    observed_search_calls=None,
                )
                if coherent
                else None
            )
            if usage is not None and isinstance(raw_usage, dict):
                alternate = dict(raw_usage)
                for primary, alias in (
                    ("input_tokens", "prompt_tokens"),
                    ("output_tokens", "completion_tokens"),
                ):
                    if primary in alternate and alias in alternate:
                        del alternate[primary]
                if (
                    _normalize_provider_usage(
                        alternate, protocol=protocol, observed_search_calls=None
                    )
                    != usage
                ):
                    usage = None  # Conflicting cache/reasoning details cannot underprice failure.
            if (
                isinstance(reported_model, str)
                and reported_model.strip()
                and len(reported_model) <= 160
                and not any(ord(c) < 32 for c in reported_model)
                and usage is not None
            ):
                receipt["usage"] = usage
        except Exception:
            receipt.pop("usage", None)  # Unavailable usage remains unpriced; never guess.
    provider_error_code = _response_provider_error_code(response)
    retry_after_seconds = _response_retry_after_seconds(response)
    if provider_error_code is not None:
        receipt["provider_error_code"] = provider_error_code
    if retry_after_seconds is not None:
        receipt["retry_after_seconds"] = retry_after_seconds
    return receipt


def _extract_sources(items: Any) -> list[Dict[str, Any]]:
    """Collect url + title + publication date from provider source objects.

    Only http/https URLs qualify (same rule as _extract_source_urls); title
    and published_date are captured when the provider supplies them and stay
    null otherwise — nothing is invented. Deduplicated by url, order kept.
    """
    if not isinstance(items, list):
        return []
    collected: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not (
            isinstance(url, str)
            and urlsplit(url).scheme in {"http", "https"}
            and urlsplit(url).netloc
        ):
            continue
        title = item.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else None
        published = None
        for key in ("published_date", "published_at", "publish_date", "date"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                published = value.strip()
                break
        collected.setdefault(url, {"url": url, "title": title, "published_date": published})
    return list(collected.values())


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
    if provider == "deepseek":
        # Official Responses text route; native web_search is not supported.
        # Reasoning remains enabled but only final assistant text is retained.
        return {
            "model": model,
            "instructions": system_prompt,
            "input": prompt,
            "reasoning": {"effort": "high"},
            "max_output_tokens": MAX_TOKENS,
            "stream": False,
            "store": False,
        }
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
        model_resolution: Any = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.provider_name = provider_name
        self.model_resolution = normalize_model_resolution(model_resolution)

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
        prompt, system_prompt, external_mode = render_external_request(prompt, system_prompt)
        requested_model = self.model
        endpoint, provider, protocol = _search_endpoint(
            self.base_url,
            requested_model,
            self.provider_name,
            external_context_only=external_mode == "external_context_only",
        )
        resolution = frozen_quick_scan_model_resolution(self.model_resolution)
        work_attempt = begin_quick_scan_send(
            prompt, system_prompt, model_requested=requested_model, model_resolution=resolution
        )
        if work_attempt is not None:
            resolution = normalize_model_resolution(work_attempt.model_resolution)
        session = http_client_manager.get_sync_session()
        headers = _search_request_headers(self.api_key, protocol)
        data = _search_request_payload(prompt, system_prompt, requested_model, provider, protocol)
        if external_mode == "external_context_only":
            for key in ("tools", "tool_choice", "include"):
                data.pop(key, None)
        attempt_id = str(uuid.uuid4())
        started_at = _utc_now()
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
            response_payload, response_sha256, response_json_basis = _canonical_http_json(response)
            parse_response = (
                _parse_external_answer_response
                if external_mode == "external_context_only"
                else _parse_protocol_search_response
            )
            parsed = parse_response(
                response,
                provider=provider,
                protocol=protocol,
                requested_model=requested_model,
                model_resolution=resolution,
                response_payload=response_payload,
            )
        except Exception as error:
            receipt = _failed_attempt_receipt(
                response,
                attempt_id,
                started_at,
                prompt,
                error,
                provider=provider,
                protocol=protocol,
                requested_model=requested_model,
                model_resolution=resolution,
            )
            if work_attempt is not None:
                work_attempt.record_failure(receipt)
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        result = _with_attempt_receipt(
            parsed,
            prompt,
            attempt_id,
            started_at,
            response,
            requested_model=requested_model,
            protocol=protocol,
            response_sha256=response_sha256,
            response_json_basis=response_json_basis,
            model_resolution=resolution,
        )
        if work_attempt is not None:
            work_attempt.record_response(
                http_status_code=_response_status_code(response),
                request_id=result.request_id,
                receipt=result.execution_metadata,
            )
            # Private transport provenance comes from this actual HTTP,
            # never from model-generated answer JSON or merged repair history.
            from src.utils.quick_scan_work_store import _sanitized_receipt

            result.execution_metadata["work_transport"] = {
                "work_attempt_id": work_attempt.attempt_id,
                "final_receipt": _sanitized_receipt(result.execution_metadata),
            }
        attach_external_use_proof(work_attempt, result.execution_metadata)
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
        model_resolution: Any = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.provider_name = provider_name
        self.model_resolution = normalize_model_resolution(model_resolution)

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
        prompt, system_prompt, external_mode = render_external_request(prompt, system_prompt)
        requested_model = self.model
        endpoint, provider, protocol = _search_endpoint(
            self.base_url,
            requested_model,
            self.provider_name,
            external_context_only=external_mode == "external_context_only",
        )
        resolution = frozen_quick_scan_model_resolution(self.model_resolution)
        work_attempt = begin_quick_scan_send(
            prompt, system_prompt, model_requested=requested_model, model_resolution=resolution
        )
        if work_attempt is not None:
            resolution = normalize_model_resolution(work_attempt.model_resolution)
        client = await http_client_manager.get_async_client()
        headers = _search_request_headers(self.api_key, protocol)
        data = _search_request_payload(prompt, system_prompt, requested_model, provider, protocol)
        if external_mode == "external_context_only":
            for key in ("tools", "tool_choice", "include"):
                data.pop(key, None)
        attempt_id = str(uuid.uuid4())
        started_at = _utc_now()
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
            response_payload, response_sha256, response_json_basis = _canonical_http_json(response)
            parse_response = (
                _parse_external_answer_response
                if external_mode == "external_context_only"
                else _parse_protocol_search_response
            )
            parsed = parse_response(
                response,
                provider=provider,
                protocol=protocol,
                requested_model=requested_model,
                model_resolution=resolution,
                response_payload=response_payload,
            )
        except Exception as error:
            receipt = _failed_attempt_receipt(
                response,
                attempt_id,
                started_at,
                prompt,
                error,
                provider=provider,
                protocol=protocol,
                requested_model=requested_model,
                model_resolution=resolution,
            )
            if work_attempt is not None:
                work_attempt.record_failure(receipt)
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        result = _with_attempt_receipt(
            parsed,
            prompt,
            attempt_id,
            started_at,
            response,
            requested_model=requested_model,
            protocol=protocol,
            response_sha256=response_sha256,
            response_json_basis=response_json_basis,
            model_resolution=resolution,
        )
        if work_attempt is not None:
            work_attempt.record_response(
                http_status_code=_response_status_code(response),
                request_id=result.request_id,
                receipt=result.execution_metadata,
            )
            # Private transport provenance comes from this actual HTTP,
            # never from model-generated answer JSON or merged repair history.
            from src.utils.quick_scan_work_store import _sanitized_receipt

            result.execution_metadata["work_transport"] = {
                "work_attempt_id": work_attempt.attempt_id,
                "final_receipt": _sanitized_receipt(result.execution_metadata),
            }
        attach_external_use_proof(work_attempt, result.execution_metadata)
        return result
