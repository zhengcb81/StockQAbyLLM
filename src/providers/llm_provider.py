#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 LLM 提供者。

使用 LLM API 分析公司并返回评分和描述。
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import requests

from src.config.settings import DISPLAY_QUERY_TRUNCATE, DISPLAY_TITLE_TRUNCATE
from src.core.models import Question, SearchResult
from src.utils.logger import get_logger
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    QuickScanWorkPersistenceError,
    QuickScanWorkUncertainError,
    bind_quick_scan_format_repair,
    external_answer_search_mode,
)

from .base_llm_provider import BaseLLMProvider, standard_answer_transport_enabled
from .llm_client import (
    LLMClient,
    LLMTransportAttemptError,
    SearchCapabilityUnavailable,
    _validate_search_generation_controls,
)
from .llm_response_parser import ParsedLLMAnswer, ParsedQuestionPack

logger = get_logger(__name__)


def _final_answer_diagnostic_policy(config_file: Any) -> Optional[dict]:
    """Explicit owner-local opt-in; never accepts an arbitrary output path."""
    runtime = Path(config_file).resolve().parent
    control = runtime / "quick_scan_final_answer_diagnostics.json"
    try:
        if control.is_symlink():
            raise ValueError()
        if not control.exists():
            return None
        if (
            not control.is_file()
            or control.resolve() != control.absolute()
            or control.stat().st_size > 8192
        ):
            raise ValueError()

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result

        value = json.loads(control.read_text(encoding="utf-8"), object_pairs_hook=unique)
        if (
            not isinstance(value, dict)
            or not {"schema_version", "enabled"}.issubset(value)
            or set(value) - {"schema_version", "enabled", "max_final_answer_bytes"}
            or value["schema_version"] != "1.0.0"
            or type(value["enabled"]) is not bool
        ):
            raise ValueError()
        maximum = value.get("max_final_answer_bytes", 262144)
        if type(maximum) is not int or not 1 <= maximum <= 1024 * 1024:
            raise ValueError()
        if value["enabled"] is False:
            return None
        directory = runtime / "final-answer-diagnostics"
        if (
            directory.is_symlink()
            or directory.resolve() != directory.absolute()
            or (directory.exists() and not directory.is_dir())
        ):
            raise ValueError()
        return {"directory": directory, "max_final_answer_bytes": maximum}
    except (OSError, ValueError, UnicodeError):
        raise ValueError("final_answer_diagnostics_configuration_invalid") from None


def _safe_pack_diagnostics(value: dict) -> dict:
    """A diagnostic cannot smuggle answer values, prompts or private channels."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "status", "rejections"}
        or value["schema_version"] != "1.0.0"
        or value["status"] not in {"rejected", "partial", "complete", "not_parsed"}
        or not isinstance(value["rejections"], list)
        or len(value["rejections"]) > 64
    ):
        raise ValueError("invalid_parser_diagnostic")
    for row in value["rejections"]:
        if (
            not isinstance(row, dict)
            or not {"code", "pointer"}.issubset(row)
            or set(row) - {"code", "pointer", "question_id"}
            or not isinstance(row["code"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", row["code"]) is None
            or not isinstance(row["pointer"], str)
            or len(row["pointer"]) > 256
            or re.fullmatch(r"(?:/[A-Za-z0-9_]+)*", row["pointer"]) is None
            or (
                "question_id" in row
                and (
                    not isinstance(row["question_id"], str)
                    or re.fullmatch(r"[A-Z0-9_]{1,160}", row["question_id"]) is None
                )
            )
        ):
            raise ValueError("invalid_parser_diagnostic")
    return value


def _final_answer_retention_rejection(content: str) -> Optional[str]:
    """Inspect the complete decoded final JSON before optional local retention.

    This is deliberately stricter than answer parsing: malformed/trailing prose
    and uninspectable nested data are hash-only. It never admits an answer or
    attempts to extract model-private channels. Decode/work limits fail closed.
    """
    private_keys = {
        "reasoning_content",
        "chain_of_thought",
        "private_thoughts",
        "thoughts",
        "analysis",
        "reasoning",
        "thinking",
    }
    markers = re.compile(
        r'(?is)<(?:think(?:ing)?|analysis|reasoning|reflection)\b|"(?:reasoning_content|chain_of_thought|private_thoughts|thoughts|analysis|reasoning|thinking)"\s*:'
    )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError()

    def load_complete(raw):
        text = raw.strip()
        if text.startswith("```"):
            fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.I | re.S)
            if fence is None:
                raise ValueError()
            text = fence.group(1)
        return json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)

    try:
        decoded = load_complete(content)
        if not isinstance(decoded, (dict, list)):
            return "non_json_final_channel"
        # Bound both tree traversal and repeated description-JSON decoding.
        stack = [(decoded, 0, 0, False)]
        nodes, characters = 0, 0
        while stack:
            value, depth, decodes, is_key = stack.pop()
            nodes += 1
            if nodes > 16384 or depth > 64 or decodes > 8:
                return "final_answer_inspection_limit"
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.casefold() in private_keys:
                        return "private_reasoning_marker"
                    stack.extend(
                        ((key, depth + 1, decodes, True), (item, depth + 1, decodes, False))
                    )
            elif isinstance(value, list):
                stack.extend((item, depth + 1, decodes, False) for item in value)
            elif isinstance(value, str):
                characters += len(value)
                if characters > max(1024, len(content) * 8):
                    return "final_answer_inspection_limit"
                if (is_key and value.casefold() in private_keys) or markers.search(value):
                    return "private_reasoning_marker"
                # Strings may themselves contain escaped JSON/Unicode, notably
                # description JSON strings. Inspect decoded content, not raw regex
                # alone. No decoded value is emitted or stored separately.
                unescaped = re.sub(
                    r"(?:\\u[0-9a-fA-F]{4})+",
                    lambda match: json.loads('"' + match.group(0) + '"'),
                    value,
                )
                if unescaped != value:
                    stack.append((unescaped, depth + 1, decodes + 1, is_key))
                visible = value.strip()
                if visible.startswith(("{", "[", '"', "```")):
                    nested = load_complete(visible)
                    stack.append((nested, depth + 1, decodes + 1, False))
        return None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return "non_json_final_channel"


def _capture_final_answer_diagnostic(
    policy: Optional[dict],
    content: str,
    metadata: dict,
    trusted: dict,
    *,
    diagnostics: Optional[dict] = None,
) -> Optional[dict]:
    """Retain only final-channel text, not a provider response/prompt/reasoning.

    The immutable artifact is diagnostic data, never owner permission, a replay
    command or an import ACK. Transport remains the only billing authority.
    Optional capture I/O cannot trigger retries or discard a valid answer.
    """
    if policy is None:
        return None
    digest = metadata.get("response_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return {"status": "unavailable", "reason": "original_response_digest_unavailable"}
    try:
        raw = content.encode("utf-8")
        final_digest = hashlib.sha256(raw).hexdigest()
        retained = len(raw) <= policy["max_final_answer_bytes"]
        retention_reason = None if retained else "final_answer_size_limit"
        if retained:
            retention_reason = _final_answer_retention_rejection(content)
            retained = retention_reason is None
        allowed = {
            key: metadata.get(key)
            for key in ("provider", "actual_model", "response_id", "response_status")
            if isinstance(metadata.get(key), str)
            and len(metadata[key]) <= 300
            and not any(ord(ch) < 32 for ch in metadata[key])
        }
        transport = {
            "question_pack_id": trusted["pack_id"],
            "execution_attempt_id": trusted["execution_attempt_id"],
            "physical_budget_attempt_id": trusted["budget_attempt_id"],
            "members": [
                {key: member[key] for key in ("question_id", "work_item_id", "work_attempt_id")}
                for member in trusted["members"]
            ],
        }
        # Filename is a hash of original references, never caller path material.
        identity = {
            "response_sha256": digest,
            "final_answer_sha256": final_digest,
            "question_pack_id": transport["question_pack_id"],
            "physical_budget_attempt_id": transport["physical_budget_attempt_id"],
        }
        name = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        artifact_name, parser_name = name + ".final.json", name + ".parser.json"
        record = {
            "schema_version": "1.0.0",
            "record_kind": "final_answer_diagnostic",
            **allowed,
            "response_sha256": digest,
            "original_transport": transport,
            "final_answer_sha256": final_digest,
            "final_answer_bytes": len(raw),
            "final_answer_retained": retained,
            "retention_reason": retention_reason,
            "final_answer": content if retained else None,
            "parser_artifact_name": parser_name,
            "replay_or_delivery_authorized": False,
        }
        directory = policy["directory"]
        if directory.is_symlink() or directory.resolve() != directory.absolute():
            raise ValueError("diagnostic_directory_changed")
        directory.mkdir(exist_ok=True)

        def save_once(filename, value):
            path = directory / filename
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError("diagnostic_path_changed")
            encoded = (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            try:
                with path.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if path.stat().st_size != len(encoded) or path.read_bytes() != encoded:
                    raise ValueError("diagnostic_artifact_collision") from None

        save_once(artifact_name, record)
        if diagnostics is not None:
            safe = _safe_pack_diagnostics(diagnostics)
            save_once(
                parser_name,
                {
                    "schema_version": "1.0.0",
                    "final_answer_sha256": final_digest,
                    "response_sha256": digest,
                    "original_transport": transport,
                    "parser_diagnostics": safe,
                    "replay_or_delivery_authorized": False,
                },
            )
        return {
            "status": "recorded",
            "artifact_name": artifact_name,
            "parser_artifact_name": parser_name,
            "final_answer_sha256": final_digest,
            "final_answer_retained": retained,
            "parser_diagnostic_saved": diagnostics is not None,
            "replay_or_delivery_authorized": False,
        }
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return {"status": "unavailable", "reason": "final_answer_diagnostic_unavailable"}


@dataclass(frozen=True)
class QuestionPackSearchResult:
    """One physical response and its independently parsed logical answers."""

    results: Tuple[SearchResult, ...]
    missing_question_ids: Tuple[str, ...]
    invalid_question_ids: Tuple[str, ...]
    execution_metadata: Dict[str, Any]


class LLMProvider(BaseLLMProvider):
    """同步 LLM 提供者类。"""

    def __init__(
        self,
        *args: Any,
        thinking_enabled: Optional[bool] = None,
        max_output_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        _validate_search_generation_controls(thinking_enabled, max_output_tokens)
        super().__init__(*args, **kwargs)
        if not self.require_search and (
            thinking_enabled is not None or max_output_tokens is not None
        ):
            raise ValueError("explicit generation controls require the verified search client")
        self.thinking_enabled = thinking_enabled
        self.max_output_tokens = max_output_tokens
        # 确保api_key和model不为None
        api_key = self.api_key or ""
        model = self.model or ""
        self.client = LLMClient(
            api_key=api_key,
            model=model,
            base_url=self.base_url,
            timeout=self.timeout,
            provider_name=self.provider_name,
            model_resolution=self.model_resolution,
        )

    @staticmethod
    def _evidenced_search_provider(execution_metadata: Dict[str, Any]) -> Optional[str]:
        """Name a transport only after a provider response or HTTP status exists."""
        candidates = [execution_metadata]
        attempts = execution_metadata.get("attempts")
        if isinstance(attempts, list):
            candidates.extend(reversed(attempts))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            provider = candidate.get("provider")
            if not isinstance(provider, str) or provider not in {
                "openai",
                "minimax",
                "mimo",
                "deepseek",
            }:
                continue
            if candidate.get("response_id") or type(candidate.get("http_status_code")) is int:
                return provider
        return None

    def search(self, query: str) -> List[SearchResult]:
        """执行同步搜索。"""
        return self._search(query, question_id=None)

    def search_question(self, question: Question) -> List[SearchResult]:
        """Execute one identified quick-scan question without inferring its ID."""
        return self._search(question.text, question_id=question.question_id)

    def _search(self, query: str, question_id: Optional[str]) -> List[SearchResult]:
        logger.info("LLM处理问题: %s...", query[:DISPLAY_QUERY_TRUNCATE])

        prompt = self._build_prompt(query, question_id=question_id)
        parsed, execution_metadata = self._call_llm_api(
            prompt,
            expected_question_id=question_id,
            expected_entity_id=self.entity_id if self.require_search else None,
            expected_company_name=self.company_name if self.require_search else None,
        )
        if self.require_search:
            execution_metadata = dict(execution_metadata)
            execution_metadata["provider_config_ref"] = self.provider_name
            attempts = execution_metadata.get("attempts")
            if isinstance(attempts, list):
                execution_metadata["attempts"] = [
                    (
                        {**attempt, "provider_config_ref": self.provider_name}
                        if isinstance(attempt, dict)
                        else attempt
                    )
                    for attempt in attempts
                ]

        result = self._result_from_parsed(query, parsed, execution_metadata)

        logger.info("LLM答案生成完成 (状态: %s)", parsed.status)
        return [result]

    def _result_from_parsed(
        self, query: str, parsed: ParsedLLMAnswer, execution_metadata: Dict[str, Any]
    ) -> SearchResult:
        from src.utils.quick_scan_work_transport import project_quick_scan_search

        search_projection = project_quick_scan_search(execution_metadata)

        return SearchResult(
            title=f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            snippet=parsed.description,
            source="llm_api",
            url="",
            rank=0,
            score=parsed.score,
            status=parsed.status,
            metadata={
                "question_id": parsed.question_id,
                "entity_id": parsed.entity_id,
                "company_name": parsed.company_name,
                "parsed_score": parsed.score,
                "information_as_of": getattr(parsed, "information_as_of", None),
                "provider": (
                    self._evidenced_search_provider(execution_metadata)
                    if self.require_search
                    else self.provider_name
                ),
                "provider_config_ref": self.provider_name,
                "model_requested": self.model,
                "request_id": execution_metadata.get("request_id"),
                "response_id": execution_metadata.get("response_id"),
                "actual_model": execution_metadata.get("actual_model"),
                "search_status": search_projection.get("search_status"),
                "search_receipt_id": search_projection.get("search_receipt_id"),
                "source_urls": search_projection.get("source_urls", []),
                "failure_type": execution_metadata.get("failure_type"),
                "execution": execution_metadata,
                "attempts": execution_metadata.get("attempts", []),
                "format_repair": execution_metadata.get("format_repair"),
            },
        )

    def _search_generation_options(self) -> Dict[str, Any]:
        options: Dict[str, Any] = {}
        if self.thinking_enabled is not None:
            options["thinking_enabled"] = self.thinking_enabled
        if self.max_output_tokens is not None:
            options["max_output_tokens"] = self.max_output_tokens
        return options

    def search_question_pack(
        self,
        questions: List[Question],
        *,
        thinking_enabled: bool = True,
        max_output_tokens: int = 8192,
    ) -> QuestionPackSearchResult:
        """Send one registered native-search pack, with no retries or format repair.

        This parser/provider step does not attest financial correctness or an
        import ACK. Each returned member retains the one physical usage receipt;
        missing and invalid members are diagnostics, never synthetic answers.
        """
        if not self.require_search:
            raise ValueError("question packs require verified search")
        if type(thinking_enabled) is not bool or type(max_output_tokens) is not int:
            raise ValueError("question pack generation controls must be explicit bool/int")
        _validate_search_generation_controls(thinking_enabled, max_output_tokens)
        if (
            not isinstance(self.entity_id, str)
            or not self.entity_id.strip()
            or self.entity_id != self.entity_id.strip()
            or not isinstance(self.company_name, str)
            or not self.company_name.strip()
            or self.company_name != self.company_name.strip()
        ):
            raise ValueError("question pack requires exact entity and company identity")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 30:
            raise ValueError("questions must be a list of 1..30 identified questions")
        if any(
            not isinstance(question, Question)
            or not isinstance(question.text, str)
            or not question.text.strip()
            or not isinstance(question.question_id, str)
            or not 1 <= len(question.question_id) <= 160
            or re.fullmatch(r"[A-Z0-9_]+", question.question_id) is None
            for question in questions
        ):
            raise ValueError("question pack members need stable uppercase question IDs")
        question_ids = tuple(cast(str, question.question_id) for question in questions)
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("question pack IDs must be unique")
        if external_answer_search_mode() is not None:
            raise QuickScanWorkPersistenceError(
                "question packs currently require native search; external evidence needs independent member proofs"
            )
        from src.utils.quick_scan_work_transport import (
            assert_question_pack_binding,
            project_question_pack_transport,
        )

        assert_question_pack_binding(question_ids, self.entity_id)
        full_standard_transport = standard_answer_transport_enabled()
        diagnostic_policy = _final_answer_diagnostic_policy(self.config_manager.config_file)
        prompt = self._build_prompt("", question_pack=questions)

        def reply(
            metadata: Dict[str, Any],
            status: str,
            results: Tuple[SearchResult, ...] = (),
            missing: Tuple[str, ...] = (),
            invalid: Tuple[str, ...] = (),
        ) -> QuestionPackSearchResult:
            returned = dict(metadata)
            returned["question_pack"] = {
                "status": status,
                "answer_status": "unknown" if not results else "independent_member_statuses",
                "question_ids": list(question_ids),
                "missing_question_ids": list(missing),
                "invalid_question_ids": list(invalid),
                "format_repair_attempted": False,
            }
            return QuestionPackSearchResult(results, missing, invalid, returned)

        if not self.api_key:
            return reply(
                {"search_status": "unavailable", "failure_type": "missing_api_key"},
                "unavailable",
                missing=question_ids,
            )
        if not self.client.supports_web_search:
            return reply(
                {"search_status": "unavailable", "failure_type": "unsupported_search"},
                "unavailable",
                missing=question_ids,
            )
        try:
            controls: Dict[str, Any] = {
                "thinking_enabled": thinking_enabled,
                "max_output_tokens": max_output_tokens,
            }
            if getattr(self.client, "supports_search_json_mode", False) is True:
                controls["json_mode"] = True
            response = self.client.send_search_request(prompt, **controls)
        except SearchCapabilityUnavailable as error:
            return reply(
                {"search_status": "unavailable", "failure_type": type(error).__name__},
                "unavailable",
                missing=question_ids,
            )
        except LLMTransportAttemptError as error:
            return reply(
                {
                    **error.attempt_receipt,
                    "attempts": [error.attempt_receipt],
                    "search_status": "unverified",
                    "failure_type": error.failure_type,
                },
                "transport_failed",
                missing=question_ids,
            )

        # Never accept the model/provider response's member map as authority.
        # The projector re-reads the active immutable group and final owner receipt.
        trusted = project_question_pack_transport(response.execution_metadata)
        members = trusted.get("members")
        if not isinstance(members, list) or [
            member.get("question_id") for member in members
        ] != list(question_ids):
            raise QuickScanWorkPersistenceError(
                "question pack owner membership differs from supplied order"
            )
        metadata = dict(response.execution_metadata)
        metadata["provider_config_ref"] = self.provider_name
        # Capture is before parsing, so an unexpected parser failure cannot
        # erase a known paid final response. Private response channels are never
        # passed to this helper; only the SDK's final content is eligible.
        captured = _capture_final_answer_diagnostic(
            diagnostic_policy, response.content, metadata, trusted
        )
        if not response.search_verified:
            diagnostic = {
                "schema_version": "1.0.0",
                "status": "not_parsed",
                "rejections": [
                    {
                        "code": (
                            "provider_response_incomplete"
                            if metadata.get("response_status") != "completed"
                            else "search_unverified"
                        ),
                        "pointer": "",
                    }
                ],
            }
            captured = _capture_final_answer_diagnostic(
                diagnostic_policy, response.content, metadata, trusted, diagnostics=diagnostic
            )
            if captured is not None:
                metadata["final_answer_diagnostic"] = captured
            metadata["question_pack_parse_diagnostics"] = diagnostic
            return reply(metadata, "search_unverified", missing=question_ids)
        parse_controls = {"standard_answer_objects": True} if full_standard_transport else {}
        try:
            parsed, diagnostic = self.parser.parse_question_pack_response_with_diagnostics(
                response.content,
                question_ids,
                expected_entity_id=self.entity_id,
                expected_company_name=self.company_name,
                **parse_controls,
            )
        except Exception:
            _capture_final_answer_diagnostic(
                diagnostic_policy,
                response.content,
                metadata,
                trusted,
                diagnostics={
                    "schema_version": "1.0.0",
                    "status": "not_parsed",
                    "rejections": [{"code": "parser_internal_error", "pointer": ""}],
                },
            )
            raise
        captured = _capture_final_answer_diagnostic(
            diagnostic_policy, response.content, metadata, trusted, diagnostics=diagnostic
        )
        if captured is not None:
            metadata["final_answer_diagnostic"] = captured
        metadata["question_pack_parse_diagnostics"] = diagnostic
        if parsed is None:
            return reply(metadata, "rejected", invalid=question_ids)
        if full_standard_transport:
            from src.utils.quick_scan_work_transport import (
                validate_question_pack_standard_answers,
            )

            standard_invalid = validate_question_pack_standard_answers(
                metadata,
                [
                    {
                        "question_id": answer.question_id,
                        "entity_id": answer.entity_id,
                        "status": answer.status,
                        "score": answer.score,
                        "description": answer.description,
                        "information_as_of": answer.information_as_of,
                    }
                    for answer in parsed.answers
                ],
            )
            all_rejections = {
                **{qid: "pack_member_missing" for qid in parsed.missing_question_ids},
                **{qid: "pack_member_invalid" for qid in parsed.invalid_question_ids},
                **standard_invalid,
            }
            valid_count = sum(
                answer.question_id not in standard_invalid for answer in parsed.answers
            )
            metadata["question_pack_standard_diagnostics"] = {
                "schema_version": "1.0.0",
                "status": (
                    "rejected" if not valid_count else ("partial" if all_rejections else "complete")
                ),
                "rejections": [
                    {"question_id": qid, "code": all_rejections[qid]}
                    for qid in question_ids
                    if qid in all_rejections
                ],
            }
            if standard_invalid:
                invalid = set(parsed.invalid_question_ids) | set(standard_invalid)
                parsed = ParsedQuestionPack(
                    tuple(
                        answer
                        for answer in parsed.answers
                        if answer.question_id not in standard_invalid
                    ),
                    parsed.missing_question_ids,
                    tuple(qid for qid in question_ids if qid in invalid),
                )
        by_id = {member["question_id"]: member for member in members}
        questions_by_id = {question.question_id: question for question in questions}
        physical = metadata.get("work_transport")
        if not isinstance(physical, dict):
            raise QuickScanWorkPersistenceError("question pack has no actual physical transport")
        results = []
        for answer in parsed.answers:
            member = by_id[answer.question_id]
            execution = dict(metadata)
            execution["work_transport"] = {
                **physical,
                "work_attempt_id": member["work_attempt_id"],
                "work_item_id": member["work_item_id"],
                "physical_budget_attempt_id": trusted["budget_attempt_id"],
                "question_pack_id": trusted["pack_id"],
            }
            execution["question_pack_member"] = {
                "pack_id": trusted["pack_id"],
                "question_id": answer.question_id,
                "work_item_id": member["work_item_id"],
                "work_attempt_id": member["work_attempt_id"],
                "execution_attempt_id": trusted["execution_attempt_id"],
                "usage_basis": "shared_physical_request_not_apportioned",
            }
            results.append(
                self._result_from_parsed(
                    questions_by_id[answer.question_id].text, answer, execution
                )
            )
        status = (
            "partial" if parsed.missing_question_ids or parsed.invalid_question_ids else "complete"
        )
        return reply(
            metadata,
            status,
            tuple(results),
            parsed.missing_question_ids,
            parsed.invalid_question_ids,
        )

    def _repair_invalid_answer(
        self,
        original_prompt: str,
        malformed_response: str,
        expected_question_id: Optional[str],
        expected_entity_id: Optional[str],
        expected_company_name: Optional[str],
        initial_metadata: Dict[str, Any],
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """Perform at most one explicitly budgeted format repair."""
        repair_prompt = self._build_format_repair_prompt(
            original_prompt,
            malformed_response,
            expected_question_id,
            expected_entity_id,
            expected_company_name,
        )
        repair_metadata: Dict[str, Any] = {}
        try:
            if self.require_search:
                with bind_quick_scan_format_repair():
                    response = self.client.send_search_request(
                        repair_prompt, **self._search_generation_options()
                    )
                repair_metadata = response.execution_metadata
                if not response.search_verified:
                    metadata = self._merge_format_repair_metadata(
                        initial_metadata, repair_metadata, status="unverified"
                    )
                    return (
                        ParsedLLMAnswer(None, "格式修复响应未通过联网搜索执行验证。", "unknown"),
                        metadata,
                    )
                repaired_content = response.content
            else:
                repaired_content = self.client.send_request(repair_prompt)

            repaired = self.parser.parse_structured_response(
                repaired_content,
                expected_question_id=expected_question_id,
                expected_entity_id=expected_entity_id,
                expected_company_name=expected_company_name,
                strict_json_only=self.require_search,
            )
            metadata = self._merge_format_repair_metadata(
                initial_metadata,
                repair_metadata,
                status="repaired" if repaired is not None else "failed",
            )
            if repaired is None:
                return (
                    ParsedLLMAnswer(None, "一次格式修复后仍无法验证结构化回答。", "unknown"),
                    metadata,
                )
            return repaired, metadata
        except Exception as error:
            if isinstance(
                error,
                (
                    QuickScanBudgetDeferredError,
                    QuickScanWorkPersistenceError,
                    QuickScanWorkUncertainError,
                ),
            ):
                raise
            logger.warning("格式修复失败（%s）", type(error).__name__)
            if isinstance(error, LLMTransportAttemptError):
                repair_metadata = {
                    **error.attempt_receipt,
                    "attempts": [error.attempt_receipt],
                    "search_status": "unverified",
                }
            metadata = self._merge_format_repair_metadata(
                initial_metadata,
                repair_metadata,
                status="failed",
                failure_type=type(error).__name__,
            )
            if self.require_search and not metadata.get("search_status"):
                metadata["search_status"] = "unavailable"
            return ParsedLLMAnswer(None, "格式修复请求失败。", "unknown"), metadata

    def _call_llm_api(
        self,
        prompt: str,
        max_retries: Optional[int] = None,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """调用 LLM API，带重试。"""
        if not self.api_key:
            logger.warning("LLM API未配置，返回占位符结果")
            return ParsedLLMAnswer(None, "LLM API未配置。请配置 API密钥。", "error"), {}

        if (
            self.require_search
            and not self.client.supports_web_search
            and external_answer_search_mode() != "external_context_only"
        ):
            return (
                ParsedLLMAnswer(
                    None, "当前提供商或端点不支持可验证的联网搜索。", "insufficient_evidence"
                ),
                {"provider": None, "search_status": "unavailable"},
            )

        # 使用提供的 max_retries 或默认值
        num_retries = max_retries if max_retries is not None else self.max_retries
        transport_attempts: List[Dict[str, Any]] = []

        for retry in range(num_retries):
            try:
                logger.info(
                    "正在调用 %s API... (尝试 %d/%d)",
                    self.provider_name,
                    retry + 1,
                    num_retries,
                )

                execution_metadata: Dict[str, Any] = {}
                if self.require_search:
                    search_response = self.client.send_search_request(
                        prompt, **self._search_generation_options()
                    )
                    execution_metadata = search_response.execution_metadata
                    if transport_attempts:
                        execution_metadata = self._prepend_attempt_history(
                            execution_metadata, transport_attempts
                        )
                    if not search_response.search_verified:
                        return (
                            ParsedLLMAnswer(
                                None,
                                "无法验证本次请求实际完成了联网搜索及来源返回。",
                                "insufficient_evidence",
                            ),
                            execution_metadata,
                        )
                    content = search_response.content
                else:
                    content = self.client.send_request(prompt)
                result = self.parser.parse_structured_response(
                    content,
                    expected_question_id=expected_question_id,
                    expected_entity_id=expected_entity_id,
                    expected_company_name=expected_company_name,
                    strict_json_only=self.require_search,
                )

                if result is not None:
                    return result, execution_metadata
                if self.require_search:
                    if self.format_repair_budget:
                        return self._repair_invalid_answer(
                            prompt,
                            content,
                            expected_question_id,
                            expected_entity_id,
                            expected_company_name,
                            execution_metadata,
                        )
                    execution_metadata = dict(execution_metadata)
                    execution_metadata["format_repair"] = {
                        "attempted": False,
                        "status": "not_enabled",
                        "failure_type": "invalid_answer",
                    }
                    return (
                        ParsedLLMAnswer(None, "无法验证结构化评分回答及目标公司身份。", "unknown"),
                        execution_metadata,
                    )
                if self.format_repair_budget:
                    return self._repair_invalid_answer(
                        prompt,
                        content,
                        expected_question_id,
                        expected_entity_id,
                        expected_company_name,
                        execution_metadata,
                    )

                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("解析失败，%.1f 秒后重试...", wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error("重试 %d 次后仍然无法解析JSON", num_retries)
                    return (
                        ParsedLLMAnswer(None, "无法解析结构化评分回答。", "error"),
                        execution_metadata,
                    )

            except (
                QuickScanBudgetDeferredError,
                QuickScanWorkPersistenceError,
                QuickScanWorkUncertainError,
            ):
                raise
            except LLMTransportAttemptError as error:
                transport_attempts.append(error.attempt_receipt)
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "API请求失败（%s），%.1f 秒后重试...", error.failure_type, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "%s API请求失败，已尝试 %d 次（%s）",
                        self.provider_name,
                        len(transport_attempts),
                        error.failure_type,
                    )
                    return (
                        ParsedLLMAnswer(None, f"联网请求失败（{error.failure_type}）。", "error"),
                        {
                            "provider": None,
                            "search_status": "unverified",
                            "attempts": transport_attempts,
                            "failure_type": error.failure_type,
                        },
                    )

            except SearchCapabilityUnavailable as e:
                return (
                    ParsedLLMAnswer(None, str(e), "insufficient_evidence"),
                    {"provider": None, "search_status": "unavailable"},
                )
            except requests.RequestException as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "API请求失败（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "%s API请求失败，已重试 %d 次（%s）",
                        self.provider_name,
                        num_retries,
                        type(e).__name__,
                    )
                    raise Exception(
                        f"API请求失败，已重试 {num_retries} 次（{type(e).__name__}）"
                    ) from e

            except Exception as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "发生未知错误（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍发生错误（%s）", num_retries, type(e).__name__)
                    return ParsedLLMAnswer(None, f"处理失败（{type(e).__name__}）", "error"), {}

        raise Exception("API调用失败：未知错误")
