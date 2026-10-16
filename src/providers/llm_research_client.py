"""Metered thinking/tool conversation for research adapters.

No orchestration, search implementation, queue or cost ledger lives here.
Only provider-produced assistant messages enter private in-memory history.
Hidden reasoning must be returned to these providers during tool rounds, but
is deliberately absent from all public results and durable receipts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from src.providers.llm_client import (
    LLMTransportAttemptError,
    _canonical_http_json,
    _failed_attempt_receipt,
    _response_request_id,
    _response_status_code,
    _utc_now,
)
from src.providers.model_resolution import model_resolution_sha256
from src.utils.http_client import http_client_manager
from src.utils.quick_scan_work_transport import (
    assert_controlled_generation_payload,
    begin_quick_scan_send,
    require_budgeted_research_send,
)


def _encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _usage(payload):
    """Missing counters stay unknown; reasoning is already inside completion."""
    raw = payload.get("usage") if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    prompt = _count(raw.get("prompt_tokens"))
    output = _count(raw.get("completion_tokens"))
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cache = _count(raw.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
    detail_cache = _count(details.get("cached_tokens"))
    reasoning_details = raw.get("completion_tokens_details")
    reasoning_details = reasoning_details if isinstance(reasoning_details, dict) else {}
    reasoning = _count(reasoning_details.get("reasoning_tokens"))
    observation = {
        "prompt_tokens": prompt,
        "cached_input_tokens": cache,
        "output_tokens_including_reasoning": output,
        "reasoning_output_tokens": reasoning,
    }
    coherent = all(v is not None for v in (prompt, output, cache, reasoning))
    coherent = coherent and cache <= prompt and reasoning <= output
    if "cached_tokens" in details and detail_cache != cache:
        coherent = False
    if "prompt_cache_miss_tokens" in raw and (
        prompt is None or cache is None or _count(raw["prompt_cache_miss_tokens"]) != prompt - cache
    ):
        coherent = False
    if "total_tokens" in raw and (
        prompt is None or output is None or _count(raw["total_tokens"]) != prompt + output
    ):
        coherent = False
    usage = None
    if coherent:
        usage = {
            "schema": "quick-scan-usage-v1",
            "input_tokens": prompt - cache,
            "cached_input_tokens": cache,
            "cache_creation_input_tokens": 0,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "search_tool_calls": 0,
        }  # These requests declare function tools only.
    return observation, usage


class ResearchChatSession:
    """One company/strategy's opaque, non-persistable provider conversation.

    Endpoint/model qualification is intentionally explicit. Owner-controlled
    generation guards still run; this class does not grant identity authority.
    A failed or uncertain step blocks this session, never automatically retries.
    """

    def __init__(
        self,
        client,
        *,
        system_prefix,
        tools,
        max_output_tokens=4096,
        session_input_tokens=240000,
        session_output_tokens=48000,
        wall_seconds=900,
    ):
        endpoint = urlsplit(client.base_url)
        allowed = {
            ("deepseek", "api.deepseek.com", "/chat/completions", "deepseek-flash"),
            ("deepseek", "api.deepseek.com", "/v1/chat/completions", "deepseek-flash"),
            ("mimo", "api.xiaomimimo.com", "/v1/chat/completions", "mimo-v2.6-pro"),
            ("mimo", "api.xiaomimimo.com", "/v1/chat/completions", "mimo-v2.6-flash"),
        }
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != endpoint.hostname
            or endpoint.query
            or endpoint.fragment
            or (client.provider_name, endpoint.hostname, endpoint.path, client.model) not in allowed
        ):
            raise ValueError("research_endpoint_not_qualified")
        if not isinstance(system_prefix, str) or not system_prefix.strip():
            raise ValueError("research_system_prefix_required")
        if (
            any(
                type(v) is not int or v < 1
                for v in (
                    max_output_tokens,
                    session_input_tokens,
                    session_output_tokens,
                    wall_seconds,
                )
            )
            or max_output_tokens > 16384
        ):
            raise ValueError("research_limits_invalid")
        if not isinstance(tools, list) or not 1 <= len(tools) <= 20:
            raise ValueError("research_function_tools_required")
        self._validators: dict[str, Any] = {}
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValueError("research_only_function_tools")
            function = tool["function"]
            name = function["name"]
            schema = function["parameters"]
            if (
                not isinstance(name, str)
                or name in self._validators
                or schema.get("type") != "object"
            ):
                raise ValueError("research_tool_definition_invalid")
            if schema.get("additionalProperties") is not False:
                raise ValueError("research_tools_must_reject_undeclared_fields")

            def local_references(value):
                if isinstance(value, dict):
                    for key, nested in value.items():
                        if key in {"$ref", "$dynamicRef"} and (
                            not isinstance(nested, str) or not nested.startswith("#")
                        ):
                            raise ValueError("research_remote_schema_references_forbidden")
                        local_references(nested)
                elif isinstance(value, list):
                    for nested in value:
                        local_references(nested)

            local_references(schema)
            Draft202012Validator.check_schema(schema)
            self._validators[name] = Draft202012Validator(schema)
        self._endpoint, self._provider, self._model = (
            client.base_url,
            client.provider_name,
            client.model,
        )
        self._api_key, self._timeout = client.api_key, client.timeout
        self._resolution = copy.deepcopy(client.model_resolution)
        self._lock = threading.Lock()
        self._tools = copy.deepcopy(tools)
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system_prefix}]
        self._pending = ()
        self._blocked = False
        self._started = time.monotonic()
        self._max_output = max_output_tokens
        self._input_limit, self._output_limit = (
            session_input_tokens,
            session_output_tokens,
        )
        self._input_used, self._output_used = 0, 0
        self._wall = wall_seconds
        self.stable_prefix_sha256 = _sha({"system_prefix": system_prefix, "tools": tools})

    def __getstate__(self):
        raise TypeError("research_session_is_memory_only")

    def close(self):
        with self._lock:
            self._messages.clear()
            self._pending = ()
            self._api_key = None
            self._blocked = True

    def step(self, *, user_context=None, tool_results=None, continuation_context=None):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("research_session_step_already_running")
        try:
            return self._step(
                user_context=user_context,
                tool_results=tool_results,
                continuation_context=continuation_context,
            )
        finally:
            self._lock.release()

    def _step(self, *, user_context=None, tool_results=None, continuation_context=None):
        if self._blocked:
            raise RuntimeError("research_session_requires_inspection_or_new_admission")
        if continuation_context is not None and (
            not self._pending
            or not isinstance(continuation_context, str)
            or not continuation_context.strip()
            or len(continuation_context) > 60000
        ):
            raise ValueError("research_continuation_context_invalid")
        messages = copy.deepcopy(self._messages)
        if self._pending:
            if (
                user_context is not None
                or not isinstance(tool_results, dict)
                or set(tool_results) != set(self._pending)
            ):
                raise ValueError("research_tool_results_do_not_match_actual_calls")
            for call_id in self._pending:
                text = tool_results[call_id]
                if not isinstance(text, str) or len(text) > 60000:
                    raise ValueError("research_tool_result_invalid")
                messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
            if continuation_context is not None:
                messages.append({"role": "user", "content": continuation_context})
        else:
            if (
                tool_results is not None
                or not isinstance(user_context, str)
                or not user_context.strip()
            ):
                raise ValueError("research_user_context_required")
            messages.append({"role": "user", "content": user_context})
        body = {
            "model": self._model,
            "messages": messages,
            "tools": copy.deepcopy(self._tools),
            "stream": False,
            "thinking": {"type": "enabled"},
            "tool_choice": "auto",
        }
        protocol = (
            "mimo_chat_completions" if self._provider == "mimo" else "deepseek_chat_completions"
        )
        body["max_completion_tokens" if self._provider == "mimo" else "max_tokens"] = (
            self._max_output
        )
        # Byte-level conservative reservation includes message framing allowance;
        # exact actual usage replaces the reservation only after a real response.
        input_reserve = len(_encode(body).encode()) + 1024 + len(messages) * 64
        remaining = self._wall - (time.monotonic() - self._started)
        if (
            remaining <= 0
            or self._input_used + input_reserve > self._input_limit
            or self._output_used + self._max_output > self._output_limit
        ):
            raise RuntimeError("research_session_resource_limit")
        require_budgeted_research_send()
        assert_controlled_generation_payload(body, provider=self._provider, protocol=protocol)
        attempt_id = str(uuid.uuid4())
        prompt = _encode(messages)
        permit = begin_quick_scan_send(
            prompt,
            messages[0]["content"],
            model_requested=self._model,
            model_resolution=self._resolution,
            execution_attempt_id=attempt_id,
            provider_actual=self._provider,
            request_payload_sha256=_sha(body),
        )
        if permit is None or permit.budget_binding is None:
            raise RuntimeError("research_send_permit_missing")
        budget_binding = permit.budget_binding
        resolution = permit.model_resolution
        wire = http_client_manager.get_metered_sync_session()
        headers = {"Content-Type": "application/json"}
        headers["api-key" if self._provider == "mimo" else "Authorization"] = (
            self._api_key if self._provider == "mimo" else "Bearer " + self._api_key
        )
        response = None
        started = _utc_now()
        http_start = time.monotonic()
        permit.consume_for_post()
        self._blocked = True  # Ambiguous/post-send exceptions must never permit a resend.
        try:
            response = wire.post(
                self._endpoint,
                headers=headers,
                json=body,
                timeout=min(self._timeout, remaining),
                allow_redirects=False,
            )
            response.raise_for_status()
            status_code = _response_status_code(response)
            if status_code is None or not 200 <= status_code < 300:
                raise ValueError("research_http_not_successful")
            payload, digest, basis = _canonical_http_json(response)
        except Exception as error:
            receipt = _failed_attempt_receipt(
                response,
                attempt_id,
                started,
                prompt,
                error,
                provider=self._provider,
                protocol=protocol,
                requested_model=self._model,
                model_resolution=resolution,
            )
            # The legacy normalizer assumes missing cache counters are zero.
            # Do not import that assumption into this experiment's failure price.
            receipt.pop("usage", None)
            if response is not None:
                try:
                    _, failed_usage = _usage(_canonical_http_json(response)[0])
                    if failed_usage is not None:
                        receipt["usage"] = failed_usage
                except Exception:
                    pass
            receipt["request_payload_sha256"] = _sha(body)
            permit.record_failure(receipt)
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        observation, usage = _usage(payload)
        reported_model = payload.get("model") if isinstance(payload, dict) else None
        reported_id = payload.get("id") if isinstance(payload, dict) else None
        if (
            not isinstance(reported_model, str)
            or len(reported_model) > 160
            or any(ord(c) < 32 for c in reported_model)
        ):
            reported_model = None
        if (
            not isinstance(reported_id, str)
            or len(reported_id) > 300
            or any(ord(c) < 32 for c in reported_id)
        ):
            reported_id = None
        receipt = {
            "provider": self._provider,
            "attempt_id": attempt_id,
            "work_attempt_id": permit.attempt_id,
            "requested_model": self._model,
            "actual_model": reported_model,
            "response_id": reported_id,
            "request_id": _response_request_id(response),
            "http_status_code": _response_status_code(response),
            "search_protocol": protocol,
            "search_status": "not_requested",
            "started_at": started,
            "completed_at": _utc_now(),
            "request_payload_sha256": _sha(body),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "response_sha256": digest,
            "response_json_basis": basis,
        }
        receipt["model_resolution_sha256"] = model_resolution_sha256(resolution)
        if usage is not None:
            receipt["usage"] = usage
        # A malformed answer still consumed this physical request and its tokens.
        permit.record_response(
            http_status_code=_response_status_code(response),
            request_id=receipt["request_id"],
            receipt=receipt,
        )
        try:
            if reported_model != self._model:
                raise ValueError("research_actual_model_mismatch")
            choices = payload["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("research_choices_invalid")
            choice = choices[0]
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError("research_role_invalid")
            finish = choice["finish_reason"]
            if finish not in {"stop", "tool_calls"}:
                raise ValueError("research_incomplete_output")
            content = message.get("content") or ""
            if (
                not isinstance(content, str)
                or "<think" in content.lower()
                or "</think" in content.lower()
            ):
                raise ValueError("research_final_content_invalid")
            calls = message.get("tool_calls") or []
            if (
                not isinstance(calls, list)
                or len(calls) > 20
                or bool(calls) != (finish == "tool_calls")
            ):
                raise ValueError("research_tool_calls_invalid")
            tools, seen = [], set()
            for call in calls:
                call_id = call["id"]
                function = call["function"]
                name = function["name"]
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or len(call_id) > 200
                    or call_id in seen
                    or call.get("type") != "function"
                    or name not in self._validators
                ):
                    raise ValueError("research_tool_identity_invalid")
                arguments = json.loads(
                    function["arguments"],
                    object_pairs_hook=_unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
                )
                self._validators[name].validate(arguments)
                tools.append({"id": call_id, "name": name, "arguments": arguments})
                seen.add(call_id)
            private: dict[str, Any] = {
                "role": "assistant",
                "content": content,
                "tool_calls": copy.deepcopy(calls),
            }
            if "reasoning_content" in message:
                if not isinstance(message["reasoning_content"], (str, type(None))):
                    raise ValueError("research_reasoning_protocol_invalid")
                private["reasoning_content"] = message["reasoning_content"]
            self._messages = messages + [private]
            self._pending = tuple(tool["id"] for tool in tools)
        except Exception as error:
            raise LLMTransportAttemptError(type(error).__name__, receipt) from None
        if usage is not None:
            self._input_used += observation["prompt_tokens"]
            self._output_used += observation["output_tokens_including_reasoning"]
            independent_task = budget_binding.independent_task is not None
            status = budget_binding.store.get_quick_scan_budget_status(
                budget_binding.policy["policy_id"],
                **({"include_isolation": True} if independent_task else {}),
            )
            self._blocked = (
                status[
                    (
                        "blocking_unreconciled_attempts"
                        if independent_task
                        else "unreconciled_attempts"
                    )
                ]
                > 0
            )
        return {
            "protocol": "stockqa.research-step/1.0.0",
            "content": content,
            "tool_calls": tools,
            "finish_reason": finish,
            "receipt": receipt,
            "usage_observation": observation,
            "elapsed_seconds": time.monotonic() - http_start,
            "stable_prefix_sha256": self.stable_prefix_sha256,
        }
