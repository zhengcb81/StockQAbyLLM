"""Q02: Anthropic-compatible continuation protocol for DeepSeek web search.

Verified facts (2026-10-07 recheck): the endpoint really performs server-side
search, ``max_uses`` does NOT bound the number of searches, and a forced tool
turn returns ``stop_reason=tool_use`` with no final answer. This module only
CLASSIFIES a response and states the admission decision — the route stays
disabled until the continuation protocol and an enforceable cost bound are
confirmed, so no final answer and no search count is ever invented.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ADMISSION_REASON",
    "classify_anthropic_messages_response",
    "continuation_admission",
    "native_search_route_supported",
]

ADMISSION_REASON = "anthropic_continuation_cost_bound_unconfirmed"


def native_search_route_supported(
    route_kind: str, *, provider: str | None = None
) -> tuple[bool, str]:
    """Which routes may be treated as NATIVE search routes at all."""
    if route_kind == "deepseek_responses":
        return False, "responses_web_search_ignored"
    if route_kind == "deepseek_anthropic":
        return False, "continuation_not_wired"
    if route_kind == "legacy_sse_mcp":
        return False, "no_admission_evidence"
    if provider in {"openai", "minimax", "mimo"} or route_kind in {"mimo", "minimax"}:
        return True, "protocol_wired"
    return False, "unknown_native_route"


def classify_anthropic_messages_response(payload: Any) -> dict[str, Any]:
    """Classify one Messages response into a bounded continuation state.

    States: ``final_answer`` (content text exists and stop_reason allows it),
    ``continue_tool_use`` (server tool results present but the model stopped to
    request more tool input), ``incomplete`` (truncated), ``invalid``.
    """
    if not isinstance(payload, Mapping):
        return {"state": "invalid", "reason": "response_not_an_object"}
    content = payload.get("content")
    if not isinstance(content, list):
        return {"state": "invalid", "reason": "content_not_a_list"}

    stop_reason = payload.get("stop_reason")
    tool_uses: list[str] = []
    tool_results: list[str] = []
    text_parts: list[str] = []
    search_sources: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "server_tool_use":
            if isinstance(block.get("id"), str):
                tool_uses.append(block["id"])
        elif block_type == "web_search_tool_result":
            result_id = block.get("tool_use_id")
            if isinstance(result_id, str):
                tool_results.append(result_id)
            for entry in block.get("content") or []:
                if isinstance(entry, Mapping):
                    url = entry.get("url")
                    if isinstance(url, str) and url.strip():
                        search_sources.append(url)
        elif block_type == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])

    # every search result must be traceable to its own tool call
    unbound = [result_id for result_id in tool_results if result_id not in tool_uses]
    final_text = "".join(text_parts).strip()

    if stop_reason == "max_tokens" or payload.get("incomplete") is True:
        return {
            "state": "incomplete",
            "reason": "truncated_response",
            "final_text": None,
            "tool_use_ids": tool_uses,
            "tool_result_ids": tool_results,
        }
    if stop_reason == "tool_use":
        # a tool result is NOT an answer — the caller must continue the SAME
        # tool_use_id chain, re-authorise the call, and settle its cost.
        return {
            "state": "continue_tool_use",
            "reason": "tool_use_without_final_answer",
            "final_text": None,
            "tool_use_ids": tool_uses,
            "tool_result_ids": tool_results,
            "unbound_tool_results": unbound,
            "search_sources": search_sources,
        }
    if not final_text:
        return {
            "state": "invalid",
            "reason": "final_answer_missing",
            "tool_use_ids": tool_uses,
            "tool_result_ids": tool_results,
        }
    if unbound:
        return {
            "state": "invalid",
            "reason": "tool_result_without_tool_use",
            "tool_use_ids": tool_uses,
            "tool_result_ids": tool_results,
        }
    return {
        "state": "final_answer",
        "reason": "final_answer_present",
        "final_text": final_text,
        "tool_use_ids": tool_uses,
        "tool_result_ids": tool_results,
        "search_sources": search_sources,
        "search_verified": bool(tool_results and search_sources),
    }


def continuation_admission(*, cost_bound_verified: bool, protocol_verified: bool) -> dict[str, Any]:
    """Admission decision for the Anthropic-compat continuation route.

    Both the exact continuation protocol and an enforceable cost bound must be
    confirmed; otherwise the route stays disabled and callers report ``partial``
    instead of a fabricated answer or search count.
    """
    admitted = bool(cost_bound_verified and protocol_verified)
    reason = (
        None if admitted else ("protocol_unverified" if not protocol_verified else ADMISSION_REASON)
    )
    return {
        "route": "deepseek_anthropic_continuation",
        "admitted": admitted,
        "reason": reason,
        "max_uses_is_not_a_cost_bound": True,
    }
