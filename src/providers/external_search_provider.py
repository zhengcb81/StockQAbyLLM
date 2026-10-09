"""One metered external retrieval HTTP operation, never an answer model.

The coordinator owns the shared budget, linked operation journal and outcome
transaction. This adapter consumes its one-shot admitted permit, performs ONE
request, and returns bounded data and truthful transport metadata. It never
retries, dispatches another route, persists raw pages or fabricates native tools.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from typing import Any

from src.config.quick_scan_search_policy import (
    EXECUTION_POLICY_SCHEMA_ID,
    EXTERNAL_ENDPOINTS,
    SearchPolicy,
    _admit,
    assert_policy_frozen,
    execution_query_plans,
)
from src.providers.external_search_parsers import (
    DEFAULT_MAX_IN_MEMORY_BYTES,
    parse_route,
    strict_search_json,
    unwrap_json_text,
)
from src.utils.http_client import http_client_manager
from src.utils.quick_scan_external_journal import _digest
from src.utils.quick_scan_mcp_journal import (
    APPROVED_SEARCH_TOOLS,
    SUPPORTED_PROTOCOLS,
    _control,
    _ready,
)
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import QuickScanSendAttempt

ADAPTER_VERSION = "stockqa.external_retrieval/1.0.0"
ENDPOINTS = EXTERNAL_ENDPOINTS
_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "quota_exceeded",
        "billing_hard_limit_reached",
        "account_quota_exceeded",
        "insufficient_funds",
        "rate_limit_exceeded",
        "too_many_requests",
        "rate_limited",
    }
)


class ExternalSearchTransportError(RuntimeError):
    """Only safe metadata leaves a failed paid transport; outcome may be unknown."""

    def __init__(self, receipt: dict[str, Any]) -> None:
        self.receipt = receipt
        super().__init__("external retrieval transport failed")


class ExternalSearchProvider:
    def __init__(self, policy: SearchPolicy, route_id: str) -> None:
        assert_policy_frozen(policy)
        if route_id not in policy.admitted:
            raise ValueError("external route is not admitted")
        route = next((item for item in policy.routes if item["route_id"] == route_id), None)
        if route is None:
            raise ValueError("external route is missing")
        self.route = copy.deepcopy(route)
        self.retrieval = copy.deepcopy(policy.retrieval)
        self.policy_sha256 = policy.policy_sha256
        self.execution_plans = (
            execution_query_plans(policy) if policy.schema_id == EXECUTION_POLICY_SCHEMA_ID else ()
        )
        kind = self.route["kind"]
        if self.route["endpoint"] != ENDPOINTS.get(kind):
            raise ValueError("external endpoint does not match the allowlisted protocol")
        cap = self.retrieval["max_in_memory_response_bytes"]
        if type(cap) is not int or not 1024 <= cap <= DEFAULT_MAX_IN_MEMORY_BYTES:
            raise ValueError("external response byte cap must be within 1024..1000000")
        ok, reason = _admit(self.route, require_storage_rights=True)
        if not ok:
            raise ValueError(reason)
        self._frozen_sha256 = self._configuration_sha256()

    def _configuration_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "route": self.route,
                    "retrieval": self.retrieval,
                    "policy_sha256": self.policy_sha256,
                    "execution_plans": self.execution_plans,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _request_id(headers: Any) -> str | None:
        value = headers.get("x-request-id") or headers.get("X-Request-Id")
        if (
            isinstance(value, str)
            and 0 < len(value.strip()) <= 300
            and not any(ord(c) < 32 for c in value)
        ):
            return value.strip()
        return None

    def _response_metadata(self, raw: bytes) -> dict[str, Any]:
        """Preserve observed vendor identifiers and units without storing body."""
        kind = self.route["kind"]
        try:
            document = unwrap_json_text(
                raw,
                max_layers=self.retrieval["max_json_unwrap_layers"],
                max_bytes=self.retrieval["max_in_memory_response_bytes"],
            )
        except (ValueError, UnicodeError):
            return {"usage_status": "invalid"} if kind == "tavily" else {}
        if not isinstance(document, dict):
            return {"usage_status": "invalid"} if kind == "tavily" else {}
        metadata: dict[str, Any] = {}
        identifier = document.get("request_id")
        if (
            isinstance(identifier, str)
            and 0 < len(identifier.strip()) <= 300
            and not any(ord(c) < 32 for c in identifier)
        ):
            metadata["provider_request_id"] = identifier.strip()
        if kind == "tavily":
            if "usage" not in document:
                metadata["usage_status"] = "missing"
            else:
                usage = document["usage"]
                credits = usage.get("credits") if isinstance(usage, dict) else None
                if type(credits) is not int or not 0 <= credits <= 9_223_372_036_854_775_807:
                    metadata["usage_status"] = "invalid"
                else:
                    metadata["usage"] = {
                        "schema": "stockqa.external_search_usage/1.0.0",
                        "unit": "credits",
                        "count": credits,
                    }
                    maximum = self.route.get("metering", {}).get("max_usage_units_per_request")
                    metadata["usage_status"] = (
                        "exceeds_verified_bound"
                        if type(maximum) is int and credits > maximum
                        else "reported"
                    )
        return metadata

    def fetch(
        self, permit: QuickScanSendAttempt, *, query: str, top_k: int, locale: str
    ) -> dict[str, Any]:
        """Read short search data; caller must durably settle the returned outcome."""
        if self._configuration_sha256() != self._frozen_sha256:
            raise ValueError("external configuration no longer matches its frozen policy")
        if (
            not isinstance(permit, QuickScanSendAttempt)
            or permit.binding is not None
            or permit.budget_binding is None
            or permit.budget_attempt_id != permit.attempt_id
            or not isinstance(permit.budget_binding.store, QuickScanWorkStore)
            or not isinstance(permit.external_operation_id, str)
            or permit.mcp_stage_id is not None
        ):
            raise ValueError("external retrieval requires a budget-only durable permit")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 2000
            or type(top_k) is not int
            or not 1 <= top_k <= 10
        ):
            raise ValueError("external query or result bound is invalid")
        if not isinstance(locale, str) or re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale) is None:
            raise ValueError("external locale is invalid")
        operation = permit.budget_binding.store.get_external_search(permit.external_operation_id)
        route = operation["context"]["route"]
        plan = operation["plan"]
        if self.execution_plans and plan not in self.execution_plans:
            raise ValueError("external intent is outside the frozen execution plan")
        if (
            operation["budget_attempt_id"] != permit.attempt_id
            or operation["budget"]["status"] != "in_flight"
            or operation["context"]["search_policy_sha256"] != self.policy_sha256
            or route["route_id"] != self.route["route_id"]
            or route["provider"] != self.route["kind"]
            or route["endpoint"] != self.route["endpoint"]
            or route["adapter_version"] != ADAPTER_VERSION
            or plan["query"] != query
            or plan["top_k"] != top_k
            or plan["locale"] != locale
        ):
            raise ValueError("external request binding does not match its frozen intent")
        mcp_binding = None
        if self.route["kind"] == "zai_mcp_streamable":
            mcp_binding = permit.budget_binding.store.get_mcp_binding(operation["cache_key"])
            frozen = operation["mcp_search_binding"]["metadata"]
            if any(
                frozen[name] != mcp_binding[name]
                for name in ("sequence_key", "stage_ids", "stage_binding_sha256s")
            ):
                raise ValueError("MCP search is not bound to its actual controls")
        key = os.environ.get(self.route["credential_env"])
        if not isinstance(key, str) or not key:
            raise ValueError("credential_env_unset")
        headers = {"Accept": "application/json"}
        kind = self.route["kind"]
        if kind == "brave":
            headers["X-Subscription-Token"] = key
            language, country = locale.split("-")
            params = {
                "q": query,
                "count": top_k,
                "country": country,
                "search_lang": "zh-hans" if locale == "zh-CN" else language,
            }
            method, data = "GET", {"params": params}
        else:
            headers["Authorization"] = "Bearer " + key
            if kind == "tavily":
                body = {
                    "query": query,
                    "max_results": top_k,
                    "search_depth": "basic",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                    "include_published_date": True,
                    "include_usage": True,
                    "auto_parameters": False,
                }
            elif kind == "zai_mcp_streamable":
                if mcp_binding is None:
                    raise ValueError("MCP controls binding required")
                headers["Accept"] = "application/json, text/event-stream"
                headers["MCP-Protocol-Version"] = mcp_binding["protocol_version"]
                if mcp_binding["session_id"] is not None:
                    if key in mcp_binding["session_id"]:
                        raise ValueError("invalid private MCP session")
                    headers["Mcp-Session-Id"] = mcp_binding["session_id"]
                body = {
                    "jsonrpc": "2.0",
                    "id": operation["operation_id"],
                    "method": "tools/call",
                    "params": {
                        "name": mcp_binding["tool_name"],
                        "arguments": {"search_query": query},
                    },
                }
            else:
                body = {
                    "search_query": query,
                    "count": top_k,
                    "search_engine": "search-prime",
                    "search_recency_filter": "noLimit",
                }
            method, data = "POST", {"json": body}
        return self._perform(
            permit,
            method=method,
            data=data,
            headers=headers,
            query=query,
            top_k=top_k,
            mcp_search=operation if mcp_binding is not None else None,
        )

    def _perform(
        self,
        permit: QuickScanSendAttempt,
        *,
        method: str,
        data: dict,
        headers: dict,
        query: str = "",
        top_k: int = 0,
        mcp_stage: dict | None = None,
        mcp_key: str | None = None,
        mcp_search: dict | None = None,
    ) -> dict[str, Any]:
        """Shared one-request transport; no implicit retries or control POSTs."""
        kind = self.route["kind"]
        session = http_client_manager.get_metered_sync_session()
        permit.consume_for_post()
        receipt: dict[str, Any] = {
            "origin": "external",
            "adapter_version": ADAPTER_VERSION,
            "route_id": self.route["route_id"],
            "route_kind": kind,
            "attempt_id": permit.attempt_id,
            "http_request_count": 1,
            "http_status_code": None,
            "request_id": None,
            "outcome": "unknown",
        }
        response = None
        try:
            response = session.request(
                method,
                self.route["endpoint"],
                headers=headers,
                timeout=30,
                stream=True,
                allow_redirects=False,
                **data,
            )
            status = response.status_code
            if type(status) is not int:
                raise ValueError("invalid_http_status")
            receipt["http_status_code"] = status
            receipt["request_id"] = self._request_id(response.headers)
            retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
            # Only a bounded, actual delta-seconds header is a known wait.
            # Date/malformed headers remain unknown rather than inventing a
            # reset from the configured cooldown or persisting raw headers.
            if (
                isinstance(retry_after, str)
                and re.fullmatch(r"[0-9]{1,10}", retry_after) is not None
                and 0 < int(retry_after) <= 100 * 365 * 86400
            ):
                receipt["retry_after_seconds"] = int(retry_after)
            expected_id = (
                mcp_search["operation_id"]
                if mcp_search is not None
                else None if mcp_stage is None else mcp_stage["message"].get("id")
            )
            raw, mcp_document = self._read_response(
                response,
                expected_id=expected_id,
                allow_sse=expected_id is not None and 200 <= status < 300,
            )
            receipt["response_body_sha256"] = hashlib.sha256(raw).hexdigest()
            receipt.update(self._response_metadata(raw))
            if not 200 <= status < 300:
                try:
                    document = strict_search_json(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    document = None
                error = document.get("error") if isinstance(document, dict) else None
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, str) and code in _ERROR_CODES:
                    receipt["provider_error_code"] = code
                if status in (401, 403, 404) or (
                    status == 429 and receipt.get("provider_error_code") in _ERROR_CODES
                ):
                    receipt["outcome"] = "confirmed_failure"
                raise ExternalSearchTransportError(receipt)
            if mcp_stage is not None:
                control = None
                try:
                    control = self._parse_mcp_control(
                        mcp_stage, raw, mcp_document, response.headers, status, mcp_key
                    )
                except (ValueError, TypeError, UnicodeError, KeyError):
                    pass
                receipt.update(
                    outcome="response_available",
                    parse_status="ok" if control is not None else "parse_failure",
                    provider_result_count=0,
                    entries=[],
                )
                return {"receipt": receipt, "control": control}
            parsed_input: bytes | dict = raw
            if mcp_search is not None:
                try:
                    document = (
                        mcp_document
                        if mcp_document is not None
                        else unwrap_json_text(
                            raw,
                            max_layers=self.retrieval["max_json_unwrap_layers"],
                            max_bytes=self.retrieval["max_in_memory_response_bytes"],
                        )
                    )
                    if not isinstance(expected_id, str):
                        raise ValueError("MCP search response ID missing")
                    rpc = self._rpc_response(document, expected_id)
                    if rpc is None:
                        raise ValueError("MCP search response missing")
                    parsed_input = rpc
                except (ValueError, TypeError, UnicodeError):
                    receipt.update(
                        outcome="response_available",
                        parse_status="parse_failure",
                        provider_result_count=0,
                        entries=[],
                    )
                    return receipt
            parsed = parse_route(
                kind,
                parsed_input,
                max_layers=self.retrieval["max_json_unwrap_layers"],
                max_bytes=self.retrieval["max_in_memory_response_bytes"],
            )
            receipt.update(
                outcome="response_available",
                parse_status=parsed["status"],
                provider_result_count=len(parsed["entries"]),
            )
            # Short data only. Raw HTTP bytes exist in memory only for the
            # bounded parse and digest above; they never enter the journal.
            receipt["entries"] = [
                dict(
                    entry,
                    query=query,
                    title=entry["title"][:500],
                    snippet=entry["snippet"][:500],
                    publisher=(
                        entry["publisher"][:500]
                        if isinstance(entry.get("publisher"), str)
                        else None
                    ),
                )
                for entry in parsed["entries"][:top_k]
                if len(entry["url"]) <= 2048
            ]
            return receipt
        except ExternalSearchTransportError:
            raise
        except Exception as error:
            receipt["failure_type"] = type(error).__name__
            raise ExternalSearchTransportError(receipt) from None
        finally:
            if response is not None:
                response.close()

    def fetch_mcp_control(self, permit: QuickScanSendAttempt) -> dict[str, Any]:
        """One actual control POST, bound to the private Q09 stage owner."""
        if (
            self._configuration_sha256() != self._frozen_sha256
            or self.route["kind"] != "zai_mcp_streamable"
        ):
            raise ValueError("MCP configuration/route binding mismatch")
        if (
            not isinstance(permit, QuickScanSendAttempt)
            or permit.binding is not None
            or permit.budget_binding is None
            or permit.budget_attempt_id != permit.attempt_id
            or not isinstance(permit.budget_binding.store, QuickScanWorkStore)
            or not isinstance(permit.mcp_stage_id, str)
            or permit.external_operation_id is not None
        ):
            raise ValueError("MCP control requires a budget-only durable permit")
        store = permit.budget_binding.store
        stage = store.get_mcp_stage(permit.mcp_stage_id)
        route = stage["context"]["route"]
        if (
            stage["budget_attempt_id"] != permit.attempt_id
            or stage["budget"]["status"] != "in_flight"
            or stage["context"]["search_policy_sha256"] != self.policy_sha256
            or route["route_id"] != self.route["route_id"]
            or route["provider"] != self.route["kind"]
            or route["endpoint"] != self.route["endpoint"]
            or route["adapter_version"] != ADAPTER_VERSION
            or self.execution_plans
            and stage["plan"] not in self.execution_plans
        ):
            raise ValueError("MCP control request binding mismatch")
        key = os.environ.get(self.route["credential_env"])
        if not isinstance(key, str) or not key:
            raise ValueError("credential_env_unset")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }
        parent_id = stage["metadata"]["parent_stage_id"]
        while parent_id is not None:
            parent = store.get_mcp_stage(parent_id)
            if not _ready(parent, store._now()):
                raise ValueError("MCP parent is no longer ready")
            if parent["stage"] == "initialize":
                binding = parent["result"]["control"]
                headers["MCP-Protocol-Version"] = binding["protocol_version"]
                if binding["session_id"] is not None:
                    if key in binding["session_id"]:
                        raise ValueError("invalid private MCP session")
                    headers["Mcp-Session-Id"] = binding["session_id"]
            parent_id = parent["metadata"]["parent_stage_id"]
        return self._perform(
            permit,
            method="POST",
            data={"json": stage["message"]},
            headers=headers,
            mcp_stage=stage,
            mcp_key=key,
        )

    @staticmethod
    def _rpc_response(document: Any, expected_id: str) -> dict | None:
        """Ignore notifications; never execute server requests or accept foreign IDs."""
        messages = document if isinstance(document, list) else [document]
        if not 1 <= len(messages) <= 20:
            raise ValueError("unsupported MCP message batch")
        found = None
        for message in messages:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid MCP envelope")
            if "method" in message:
                if "id" in message or not isinstance(message["method"], str):
                    raise ValueError("unsupported MCP server request")
                continue
            if (
                set(message) - {"jsonrpc", "id", "result", "error"}
                or message.get("id") != expected_id
                or ("result" in message) == ("error" in message)
                or found is not None
            ):
                raise ValueError("MCP response ID/shape mismatch")
            found = message
        return found

    def _read_response(
        self, response: Any, *, expected_id: str | None, allow_sse: bool
    ) -> tuple[bytes, dict | None]:
        cap = self.retrieval["max_in_memory_response_bytes"]
        content_type = (
            response.headers.get("Content-Type") or response.headers.get("content-type") or ""
        )
        is_sse = (
            allow_sse
            and isinstance(content_type, str)
            and content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
        )
        parts, size, pending, frame_count = [], 0, b"", 0
        data_lines: list[bytes] = []
        if is_sse and expected_id is None:
            raise ValueError("MCP SSE response ID missing")
        deadline = time.monotonic() + 30
        for part in response.iter_content(chunk_size=8192):
            if not isinstance(part, bytes):
                raise ValueError("invalid_response_chunk")
            size += len(part)
            if size > cap or time.monotonic() > deadline:
                raise ValueError("response_exceeds_in_memory_or_time_cap")
            parts.append(part)
            if not is_sse:
                continue
            pending += part
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    frame_count += 1
                    if frame_count > 100:
                        raise ValueError("MCP SSE frame cap exceeded")
                    if data_lines:
                        document = strict_search_json(b"\n".join(data_lines).decode("utf-8"))
                        if expected_id is None:
                            raise ValueError("MCP SSE response ID missing")
                        found = self._rpc_response(document, expected_id)
                        data_lines = []
                        if found is not None:
                            return b"".join(parts), found
                elif line.startswith(b"data:"):
                    data_lines.append(line[5:].removeprefix(b" "))
                elif line.startswith(b"event:") and line[6:].strip() != b"message":
                    raise ValueError("unsupported MCP SSE event")
        if is_sse:
            raise ValueError("MCP SSE ended without correlated response")
        return b"".join(parts), None

    def _parse_mcp_control(
        self,
        stage: dict,
        raw: bytes,
        document: dict | None,
        headers: Any,
        status: int,
        key: str | None,
    ) -> dict:
        if stage["stage"] == "initialized":
            if status != 202 or raw:
                raise ValueError("MCP notification requires empty 202")
            return {"initialized": True}
        if document is None:
            document = unwrap_json_text(
                raw,
                max_layers=self.retrieval["max_json_unwrap_layers"],
                max_bytes=self.retrieval["max_in_memory_response_bytes"],
            )
        rpc = self._rpc_response(document, stage["message"]["id"])
        if rpc is None or "error" in rpc or not isinstance(rpc.get("result"), dict):
            raise ValueError("MCP control response unsuccessful")
        result = rpc["result"]
        if stage["stage"] == "initialize":
            protocol = result.get("protocolVersion")
            capabilities = result.get("capabilities")
            server_info = result.get("serverInfo")
            if (
                not isinstance(protocol, str)
                or protocol not in SUPPORTED_PROTOCOLS
                or not isinstance(capabilities, dict)
                or not isinstance(capabilities.get("tools"), dict)
                or not isinstance(server_info, dict)
                or not isinstance(server_info.get("name"), str)
                or not isinstance(server_info.get("version"), str)
            ):
                raise ValueError("MCP required capability/protocol unavailable")
            session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
            if session is not None and (not isinstance(session, str) or key and key in session):
                raise ValueError("invalid private MCP session")
            control = {"protocol_version": protocol, "session_id": session}
            # Use the same exact typed bounds as the durable journal.
            checked = _control(
                control, "initialize", {"outcome": "response_available", "parse_status": "ok"}
            )
            if checked is None:
                raise ValueError("MCP initialize binding missing")
            return checked
        tools = result.get("tools")
        if not isinstance(tools, list) or not 1 <= len(tools) <= 100 or result.get("nextCursor"):
            raise ValueError("MCP tool discovery unsupported")
        matches = [
            tool
            for tool in tools
            if isinstance(tool, dict)
            and isinstance(tool.get("name"), str)
            and tool["name"] in APPROVED_SEARCH_TOOLS
        ]
        if len(matches) != 1:
            raise ValueError("MCP approved tool missing or ambiguous")
        tool = matches[0]
        schema = tool.get("inputSchema")
        self._validate_mcp_query_schema(schema, stage["plan"]["query"])
        return {"tool_name": tool["name"], "input_schema_sha256": _digest(schema)}

    @staticmethod
    def _validate_mcp_query_schema(schema: Any, query: str) -> None:
        """Support the discovered query-only call; no remote refs or invented count."""
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or set(schema)
            - {
                "type",
                "properties",
                "required",
                "additionalProperties",
                "title",
                "description",
                "$schema",
                "$id",
            }
            or not isinstance(schema.get("properties"), dict)
            or schema.get("required") != ["search_query"]
            or type(schema.get("additionalProperties", True)) is not bool
        ):
            raise ValueError("unsupported MCP input schema")
        field = schema["properties"].get("search_query")
        if (
            not isinstance(field, dict)
            or field.get("type") != "string"
            or set(field) - {"type", "description", "title", "minLength", "maxLength", "enum"}
        ):
            raise ValueError("unsupported MCP query schema")
        for name, valid in (
            ("minLength", lambda n: len(query) >= n),
            ("maxLength", lambda n: len(query) <= n),
        ):
            if name in field and (
                type(field[name]) is not int or field[name] < 0 or not valid(field[name])
            ):
                raise ValueError("MCP query exceeds discovered bounds")
        if "enum" in field and (
            not isinstance(field["enum"], list)
            or not 1 <= len(field["enum"]) <= 100
            or any(not isinstance(v, str) for v in field["enum"])
            or query not in field["enum"]
        ):
            raise ValueError("MCP query outside discovered enum")
