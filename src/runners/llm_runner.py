#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM运行器。

提供LLM模式的运行逻辑，使用LLM API进行分析。
"""

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from src.cli.batch_processor import (
    calculate_questions_to_process,
    load_existing_answers,
    load_questions,
    process_single_stock_with_retry,
    validate_and_repair_existing_file,
)
from src.config.config_manager import ConfigManager
from src.config.config_provider import ConfigProvider
from src.config.json_config_manager import JSONConfigManager
from src.config.llm_config import LLMConfig
from src.core.exceptions import ProcessingError
from src.core.models import Question
from src.core.qa_engine import QAEngine
from src.interfaces.search_provider import SearchProvider
from src.providers.llm_provider import LLMProvider
from src.services.answer_generator import AnswerGenerator
from src.utils.llm_integration import OrderedSearchProviderCascade
from src.utils.logger import get_logger
from src.utils.quick_scan_cost_resolver import QuickScanCostResolver
from src.utils.quick_scan_provider_health import QuickScanProviderHealth
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import bind_quick_scan_budget

logger = get_logger(__name__)


def routing_fingerprint_for(questions: List[Any]) -> str:
    """64-hex routing fingerprint derived from question TEXTS.

    P0-1 (r1 review): never ``json.dumps`` Question objects — that raised
    ``TypeError: Object of type Question is not JSON serializable`` and the
    production activation path crashed before the first question.
    """
    texts = [q.text if isinstance(q, Question) else str(q) for q in questions]
    return hashlib.sha256(
        json.dumps(texts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_identity_snapshot(
    path: str, *, expected_entity_id: Optional[str] = None
) -> Dict[str, Any]:
    """Load a W04 ``identity-export-g2b`` package into work-store identity
    fields (Q06 option 1 production activation).

    The snapshot SHA-256 is computed over the exact file bytes so the work
    item's ``identity_snapshot_sha256`` pins the identity as-of that export.
    ``expected_entity_id`` (the ``--entity-id`` argument) is cross-checked
    against the package payload — a mismatch fails fast (r1 P1-2).
    """
    raw = Path(path).read_bytes()
    package = json.loads(raw.decode("utf-8"))
    if package.get("object_type") != "entity" or package.get("schema_version") != "2.2.0":
        raise ValueError("identity snapshot: unsupported package type/version")
    payload = package.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("identity snapshot: missing payload")
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("identity snapshot: missing payload entity_id")
    if expected_entity_id is not None and entity_id != expected_entity_id:
        raise ValueError(
            f"identity snapshot entity {entity_id} does not match --entity-id {expected_entity_id}"
        )
    listings = payload.get("listings") or []
    refs: List[str] = []
    for listing in listings:
        ref = listing.get("source_binding_ref")
        if isinstance(ref, str) and ref and ref not in refs:
            refs.append(ref)
    if not refs:
        raise ValueError("identity snapshot: no source binding refs")
    identity_revision = payload.get("identity_revision")
    identity_state = payload.get("identity_state")
    if type(identity_revision) is not int or identity_revision < 1:
        raise ValueError("identity snapshot: invalid identity_revision")
    if identity_state not in {"provisional", "verified"}:
        raise ValueError("identity snapshot: invalid identity_state")
    if identity_state == "provisional" and len(refs) != 1:
        # r1 P1-3: fail fast here instead of per-question refusals at the store
        # (provisional identity requires exactly one source binding).
        raise ValueError(
            "identity snapshot: provisional identity requires exactly one source binding"
        )
    return {
        "entity_id": entity_id,
        "identity_revision": identity_revision,
        "source_binding_version": 1,
        "identity_state": identity_state,
        "source_binding_ref": refs[0],
        "source_binding_refs": refs,
        "identity_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    }


class QuickScanWorkLifecycle:
    """Q06：把公共题路径绑定到 QuickScanWorkStore 的逐题生命周期。

    契约（QAEngine 注入用）：
    * ``before_question(question) -> dict``——按九元逻辑键 create_or_attach
      并 claim（租约）+ prepare_attempt + mark_send_intent（transport 语义：
      prepare→intent 在派发前提交）；成功返回 ``{"claimed": True,
      "work_item_id", "lease", "attempt_id"}``，拒绝/非 pending 返回
      ``{"claimed": False, "reason", ...}``（调用方不派发该题）。
      **不得抛出**：store 错误一律降级为拒绝（reason=store_error:...）。
    * ``after_question(handle, result)`` / ``after_question_failed(handle, msg)``
      ——record **outcome="unknown"**（r1 P0-3：引擎缝隙无 transport 回执，
      response_available 需真 2xx+回执、confirmed_failure 需可识别 provider
      拒绝，均不得臆造）→ attempt=uncertain、work_item=uncertain 清租约：
      claim 永不可再领（JOB-10 不重派发）、C04 视 uncertain 为 resume 而非
      重问；Q07 answer checkpoint 落地后可升级为 result_ready+内容寻址证据。
      记录失败静默记日志（store 对账兜底）。

    身份字段由调用方注入（生产路径 ``--identity-snapshot``=W04 公开导出；
    ``source_binding_version`` 取 1——W04 导出不含该字段，此处显式披露
    （r1 P1-3），待权威版本随导出提供后替换）。``scope``/``scope_id`` 默认
    entity 级（scope_id 回落 entity_id），listing 级由调用方显式传入。
    ``prompt_sha256`` = sha256(``identity_snapshot_sha256|题面文本``)——
    **渲染前代理**，渲染后真实哈希属 transport 层。``run_id`` 为 UTC 时间戳
    派生、``scan_id`` 调用方给定（当前生产构造传 "scan-l02"）。
    """

    def __init__(
        self,
        store: QuickScanWorkStore,
        *,
        entity_id: str,
        run_id: str,
        scan_id: str,
        identity: Dict[str, Any],
        generation: int = 1,
        lease_seconds: float = 600.0,
        routing_fingerprint: str = "d" * 64,
        route_id: str = "cli",
        provider_name: str = "quick-scan-cli",
        model_requested: str = "quick-scan",
        scope: str = "entity",
        scope_id: Optional[str] = None,
        budget_policy: Optional[Dict[str, Any]] = None,
        budget_route: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
        c06_authority: Optional[Dict[str, Any]] = None,
        scope_by_question: Optional[Dict[str, Dict[str, str]]] = None,
        generation_by_question: Optional[Dict[str, int]] = None,
        routing_fingerprint_by_question: Optional[Dict[str, str]] = None,
        transport_managed: bool = False,
    ) -> None:
        if (budget_policy is None) != (budget_route is None):
            raise ValueError("budget_policy and budget_route must be supplied together")
        self._store = store
        self._transport_managed = transport_managed
        self._entity_id = entity_id
        self._run_id = run_id
        self._scan_id = scan_id
        self._identity = dict(identity)
        self._generation = generation
        self._lease_seconds = lease_seconds
        self._routing_fingerprint = routing_fingerprint
        self._route_id = route_id
        self._provider_name = provider_name
        self._model_requested = model_requested
        self._scope = scope
        self._scope_id = scope_id or entity_id
        self._budget_policy = budget_policy
        self._budget_route = budget_route
        self._deadline = deadline
        self._c06_authority = c06_authority
        # Q13: the frozen manifest may bind a question to a different scope or
        # a newer generation than the run default; both stay per-question and
        # never rebind an already-frozen work item.
        self._scope_by_question = dict(scope_by_question or {})
        self._generation_by_question = dict(generation_by_question or {})
        self._routing_fingerprint_by_question = dict(routing_fingerprint_by_question or {})

    def _dispatch_binding(self, question_id: str) -> tuple[str, str, int]:
        """Resolve one question's frozen scope and generation (Q13).

        A manifest question whose scope cannot be bound (no authoritative
        listing/segment ID) is a bounded refusal — never a silent fall-back to
        entity scope, which would export the wrong scope in the C06 package.
        """
        binding = self._scope_by_question.get(question_id)
        generation = self._generation_by_question.get(question_id, self._generation)
        if binding is None:
            return self._scope, self._scope_id, generation
        scope = binding.get("scope")
        scope_id = binding.get("scope_id")
        if (
            scope not in {"entity", "security", "segment"}
            or not isinstance(scope_id, str)
            or not scope_id
        ):
            raise ValueError(f"question scope binding is unresolved for {question_id}")
        if scope == "entity" and scope_id != self._entity_id:
            raise ValueError("entity scope_id must equal the run entity")
        return scope, scope_id, generation

    def before_question(self, question: Question) -> Dict[str, Any]:
        # Q09 step3: the time cap stops NEW dispatch only — no claim, no
        # request, and the work item stays pending (already-claimed work
        # keeps settling through its normal after_* path).
        if self._deadline is not None and time.monotonic() > self._deadline:
            logger.warning("时间上限已到，停止新派发")
            return {"claimed": False, "reason": "time_cap_reached"}

        text = question.text.strip()
        question_id = question.question_id or (
            "Q_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12].upper()
        )
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            scope, scope_id, generation = self._dispatch_binding(question_id)
            row = self._store.create_or_attach(
                entity_id=self._entity_id,
                question_id=question_id,
                generation=generation,
                scope=scope,
                scope_id=scope_id,
                identity_revision=self._identity["identity_revision"],
                source_binding_version=self._identity["source_binding_version"],
                identity_state=self._identity["identity_state"],
                source_binding_ref=self._identity["source_binding_ref"],
                source_binding_refs=list(self._identity["source_binding_refs"]),
                identity_snapshot_sha256=self._identity["identity_snapshot_sha256"],
                question_fingerprint=fingerprint,
                routing_fingerprint=self._routing_fingerprint_by_question.get(
                    question_id, self._routing_fingerprint
                ),
                run_id=self._run_id,
                scan_id=self._scan_id,
            )
            work_item_id = row["work_item_id"]
            lease = self._store.claim(work_item_id, lease_seconds=self._lease_seconds)
        except Exception as exc:  # noqa: BLE001 - store refusals degrade to skip
            logger.warning("work claim 异常（%s: %s），本题不派发", type(exc).__name__, exc)
            return {
                "claimed": False,
                "reason": f"store_error:{type(exc).__name__}:{exc}",
            }
        if lease is None:
            logger.warning("work item %s 非 pending，本题不派发", work_item_id)
            return {
                "claimed": False,
                "reason": "work_item_not_pending",
                "work_item_id": work_item_id,
            }
        if self._transport_managed:
            return {
                "claimed": True,
                "reason": "claimed",
                "work_item_id": work_item_id,
                "lease": lease,
            }
        prompt_sha = hashlib.sha256(
            (self._identity.get("identity_snapshot_sha256", "") + "|" + text).encode("utf-8")
        ).hexdigest()
        # Mirror the transport's request-identity derivation exactly: the key
        # must be unique per (work_item + frozen request inputs) — the store
        # rejects a key already used by another work item (r1 fix: scope must
        # be part of uniqueness, so include work_item_id like the transport).
        request_cache_key = (
            "REQ_"
            + hashlib.sha256(
                json.dumps(
                    {
                        "work_item_id": work_item_id,
                        "route_id": self._route_id,
                        "provider": self._provider_name,
                        "model_requested": self._model_requested,
                        "prompt_sha256": prompt_sha,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        try:
            attempt = self._store.prepare_attempt(
                work_item_id,
                lease,
                route_id=self._route_id,
                provider=self._provider_name,
                model_requested=self._model_requested,
                request_cache_key=request_cache_key,
                prompt_sha256=prompt_sha,
            )
            # P0-3: commit send intent before dispatch (transport semantics:
            # prepare -> mark_send_intent -> record outcome). Budget policy and
            # route are supplied together (Q09 wiring seam); both None keeps
            # the Q06 probe semantics (no work-side reserve at mark time —
            # bind_quick_scan_budget's dispatch-time reserve still covers the
            # actual HTTP call).
            self._store.mark_send_intent(
                work_item_id,
                lease,
                attempt["attempt_id"],
                budget_policy=self._budget_policy,
                budget_route=self._budget_route,
            )
            if self._budget_policy is not None:
                # Q09: we now hold our own route slot — the cascade's busy
                # wait must not deadlock on it (admission stays authoritative).
                from src.utils.quick_scan_work_transport import _OWN_RESERVATION

                _OWN_RESERVATION.set(True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("attempt prepare/intent 失败（%s），本题不派发", exc)
            return {
                "claimed": False,
                "reason": f"attempt_intent_failed:{type(exc).__name__}:{exc}",
                "work_item_id": work_item_id,
            }
        return {
            "claimed": True,
            "reason": "claimed",
            "work_item_id": work_item_id,
            "lease": lease,
            "attempt_id": attempt["attempt_id"],
        }

    def hydrate_question(self, question: Question) -> Optional[Dict[str, Any]]:
        """Q07: the stored answer checkpoint for this question's logical work
        item (JOB-03/PAR-04) — an idempotent ATTACH plus a read-only lookup,
        never a claim: the engine hydrates instead of re-dispatching. Never
        raises (the claim path would create the same logical row anyway)."""
        text = question.text.strip()
        question_id = question.question_id or (
            "Q_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12].upper()
        )
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            scope, scope_id, generation = self._dispatch_binding(question_id)
            row = self._store.create_or_attach(
                entity_id=self._entity_id,
                question_id=question_id,
                generation=generation,
                scope=scope,
                scope_id=scope_id,
                identity_revision=self._identity["identity_revision"],
                source_binding_version=self._identity["source_binding_version"],
                identity_state=self._identity["identity_state"],
                source_binding_ref=self._identity["source_binding_ref"],
                source_binding_refs=list(self._identity["source_binding_refs"]),
                identity_snapshot_sha256=self._identity["identity_snapshot_sha256"],
                question_fingerprint=fingerprint,
                routing_fingerprint=self._routing_fingerprint_by_question.get(
                    question_id, self._routing_fingerprint
                ),
                run_id=self._run_id,
                scan_id=self._scan_id,
            )
            checkpoint = self._store.get_answer_checkpoint(row["work_item_id"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("hydrate check skipped (%s)", exc)
            return None
        if checkpoint:
            logger.info("Q07 水合已存检查点（%s）", question_id)
        return checkpoint

    def _seal_delivery(self, work_item_id: str) -> None:
        """Q10: settle a freshly persisted checkpoint into a sealed C06 package
        or a durable block (authority/fields the adapter refuses to invent).

        Never raises: the checkpoint stays ``result_ready`` either way and the
        public ``--seal-deliveries`` entry re-runs this with zero model calls.
        """
        try:
            from src.utils.quick_scan_delivery_seal import seal_result_delivery

            outcome = seal_result_delivery(self._store, work_item_id, authority=self._c06_authority)
            logger.info("Q10 检查点落定 → %s（%s）", outcome["action"], work_item_id)
        except Exception as exc:  # noqa: BLE001 - a seal gap must not lose the answer
            logger.error("检查点封存未完成（%s）——交由 --seal-deliveries 补封", exc)

    def dispatch_context(self, handle: Dict[str, Any]):
        """Bind the claimed logical item; each actual POST owns its own attempt."""
        from contextlib import ExitStack, nullcontext

        from src.utils.quick_scan_work_transport import (
            bind_quick_scan_route,
            bind_quick_scan_work,
        )

        if not self._transport_managed:
            return nullcontext()
        context = ExitStack()
        context.enter_context(
            bind_quick_scan_work(self._store, handle["work_item_id"], handle["lease"])
        )
        context.enter_context(
            bind_quick_scan_route(
                route_id=self._route_id,
                provider=self._provider_name,
                model_requested=self._model_requested,
            )
        )
        return context

    def _save_transport_checkpoint(self, handle: Dict[str, Any], result: Any) -> None:
        """Save only the transport's final successful attempt; never record it twice."""
        try:
            from src.core.models import execution_receipt_for_checkpoint

            answer = getattr(result, "answer", None)
            metadata = getattr(answer, "metadata", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            execution = metadata.get("execution")
            execution = execution if isinstance(execution, dict) else {}
            transport = execution.get("work_transport")
            if not isinstance(transport, dict):
                raise ValueError("missing authoritative transport binding")
            attempt_id = transport.get("work_attempt_id")
            receipt = execution_receipt_for_checkpoint(metadata)
            if not isinstance(attempt_id, str) or receipt is None:
                raise ValueError("missing final successful transport receipt")
            payload = {
                "entity_id": self._entity_id,
                "question_id": getattr(getattr(result, "question", None), "question_id", None),
                "status": getattr(answer, "status", None),
                "score": getattr(answer, "score", None),
                "description": getattr(answer, "text", ""),
            }
            _preflight_checkpoint(payload, receipt)
            # Store checks same work/lease/model/request/hash and successful
            # phase atomically. A forged final ID or receipt cannot save.
            self._store.save_answer_checkpoint(
                handle["work_item_id"],
                handle["lease"],
                attempt_id,
                answer=payload,
                execution_receipt=receipt,
            )
            self._seal_delivery(handle["work_item_id"])
        except Exception as exc:  # noqa: BLE001 - retain authoritative transport state
            logger.warning("transport checkpoint 拒绝（%s）——原请求状态保留", exc)
            raise ProcessingError("快扫答案未能保存检查点；请恢复或核对本次回执") from exc

    def after_question(self, handle: Dict[str, Any], result: Any) -> None:
        if self._transport_managed:
            self._save_transport_checkpoint(handle, result)
            return
        try:
            from src.utils.quick_scan_work_transport import _OWN_RESERVATION

            _OWN_RESERVATION.set(False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("own-reservation flag reset skipped (%s)", exc)
        # Q07 layering (r2 P2-r2-1: no absolute claims): with a
        # checkpoint-quality receipt the store closes the attempt
        # (response_available) and persists the answer. EVERY deterministic
        # input precondition is pre-checked before any state is recorded
        # (verified identity, answer shape/I05, the route this attempt
        # prepared, plus the full save input surface via _preflight); the
        # non-preflightable window after a committed record (crash / lease
        # race / DB error) is never double-recorded and is handed to lease
        # recovery. Anything less stays on the Q06 honest-unknown path
        # (no fabricated success).
        if result is not None and self._identity.get("identity_state") == "verified":
            receipt = None
            answer_payload = None
            try:
                from src.core.models import execution_receipt_for_checkpoint

                receipt = execution_receipt_for_checkpoint(
                    getattr(getattr(result, "answer", None), "metadata", None)
                )
                question_id = getattr(getattr(result, "question", None), "question_id", None)
                answer = getattr(result, "answer", None)
                if receipt is not None and question_id and answer is not None:
                    status = getattr(answer, "status", None)
                    score = getattr(answer, "score", None)
                    shape_ok = (
                        status
                        in {
                            "scored",
                            "unknown",
                            "insufficient_evidence",
                            "not_applicable",
                        }
                        and (
                            type(score) is int and 1 <= score <= 10
                            if status == "scored"
                            else score is None
                        )
                        and receipt.get("actual_model") == self._model_requested
                    )
                    if shape_ok:
                        answer_payload = {
                            "entity_id": self._entity_id,
                            "question_id": question_id,
                            "status": status,
                            "score": score,
                            "description": getattr(answer, "text", ""),
                        }
            except Exception as exc:  # noqa: BLE001
                logger.debug("checkpoint receipt build skipped (%s)", exc)
            if receipt is not None and answer_payload is not None:
                # r1 P1-1: the FULL deterministic save surface runs first, with
                # zero state recorded — a rejected input falls back to the
                # honest-unknown path instead of stranding the attempt between
                # response_available and a missing checkpoint.
                try:
                    _preflight_checkpoint(answer_payload, receipt)
                except ValueError as exc:
                    logger.warning("检查点预检拒绝（%s）——零状态降级为诚实 unknown", exc)
                    receipt = None
            if receipt is not None and answer_payload is not None:
                recorded = False
                try:
                    from src.utils.quick_scan_work_store import (
                        quick_scan_receipt_sha256,
                    )

                    receipt_sha256 = quick_scan_receipt_sha256(receipt)
                    self._store.record_attempt_outcome(
                        handle["work_item_id"],
                        handle["lease"],
                        handle["attempt_id"],
                        outcome="response_available",
                        receipt_sha256=receipt_sha256,
                        http_status_code=receipt["http_status_code"],
                        request_id=receipt.get("request_id"),
                    )
                    recorded = True
                    self._store.save_answer_checkpoint(
                        handle["work_item_id"],
                        handle["lease"],
                        handle["attempt_id"],
                        answer=answer_payload,
                        execution_receipt=receipt,
                    )
                    logger.info("Q07 检查点已存（%s）", answer_payload["question_id"])
                    self._seal_delivery(handle["work_item_id"])
                    return
                except Exception as exc:  # noqa: BLE001
                    if recorded:
                        # phase already committed to response_available: never
                        # double-record; lease recovery owns the rest.
                        logger.error("检查点已收执但保存异常（%s）——交由恢复流程", exc)
                        return
                    logger.warning("收执记录未完成（%s）——降级为诚实 unknown", exc)
        try:
            # P0-3/r1: response_available requires a REAL transport receipt +
            # 2xx (store rejects fabrication); at the engine seam without a
            # checkpoint-quality receipt the honest outcome is "unknown" ->
            # attempt phase uncertain, work_item->uncertain (lease cleared:
            # claim can never re-dispatch it; C04 treats uncertain as resume,
            # not a fresh dispatch).
            self._store.record_attempt_outcome(
                handle["work_item_id"],
                handle["lease"],
                handle["attempt_id"],
                outcome="unknown",
            )
        except Exception as exc:  # noqa: BLE001 - 记录失败交由 store 对账兜底
            logger.error("record_attempt_outcome 失败（%s）", exc)

    def after_question_failed(self, handle: Dict[str, Any], error_message: str) -> None:
        if self._transport_managed:
            # HTTP already persisted failure/unknown. If no HTTP was sent,
            # existing fenced lease recovery proves unsent and releases it.
            return
        try:
            from src.utils.quick_scan_work_transport import _OWN_RESERVATION

            _OWN_RESERVATION.set(False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("own-reservation flag reset skipped (%s)", exc)
        try:
            # P0-3: confirmed_failure requires a recognized provider refusal
            # (store rejects unclassified failures); without transport evidence
            # the honest outcome is "unknown" as well.
            self._store.record_attempt_outcome(
                handle["work_item_id"],
                handle["lease"],
                handle["attempt_id"],
                outcome="unknown",
                failure_category="dispatch_error",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("record_attempt_outcome(failed) 失败（%s）", exc)


def _preflight_checkpoint(answer_payload: Dict[str, Any], receipt: Dict[str, Any]) -> None:
    """Run EVERY input validation ``save_answer_checkpoint`` applies BEFORE any
    state is recorded (r1 P1-1), using the store's own validators so the
    pre-flight and the save can never disagree. Raises ValueError when the
    answer/receipt is not checkpoint-quality — the caller then falls back to
    the Q06 honest-unknown path with zero recorded state (no strand)."""
    from src.utils.quick_scan_work_store import (
        _ENTITY_ID,
        _canonical_source_urls,
        _safe,
        _safe_text,
        _sanitized_receipt,
        _timestamp,
        quick_scan_receipt_sha256,
    )

    if set(answer_payload) != {
        "entity_id",
        "question_id",
        "status",
        "score",
        "description",
    }:
        raise ValueError("answer must match the normalized checkpoint contract")
    if not _ENTITY_ID.fullmatch(_safe(answer_payload["entity_id"], "entity_id")):
        raise ValueError("invalid answer entity_id")
    _safe(answer_payload["question_id"], "question_id")
    if answer_payload["status"] not in {
        "scored",
        "unknown",
        "insufficient_evidence",
        "not_applicable",
    }:
        raise ValueError("answer status is not reusable")
    score = answer_payload["score"]
    if answer_payload["status"] == "scored":
        if type(score) is not int or not 1 <= score <= 10:
            raise ValueError("scored answer requires an integer score from 1 to 10")
    elif score is not None:
        raise ValueError("non-scored answer must have a null score")
    _safe_text(answer_payload["description"], "answer description", maximum=5000)
    if not isinstance(receipt, dict):
        raise ValueError("execution receipt is required")
    sanitized = _sanitized_receipt(receipt)
    if not sanitized:
        raise ValueError("execution receipt is empty")
    if sanitized.get("search_status") != "executed":
        raise ValueError("answer checkpoint requires a verified search")
    if sanitized.get("provider") not in {"openai", "minimax", "mimo"}:
        raise ValueError("execution receipt has no verified provider")
    _safe_text(sanitized.get("actual_model"), "actual_model", maximum=160)
    _safe_text(sanitized.get("response_id"), "response_id", maximum=300)
    _safe_text(sanitized.get("attempt_id"), "attempt_id", maximum=300)
    _safe_text(sanitized.get("search_receipt_id"), "search_receipt_id", maximum=300)
    if sanitized.get("response_status") != "completed":
        raise ValueError("execution receipt response is not complete")
    http_status_code = sanitized.get("http_status_code")
    if type(http_status_code) is not int or not 200 <= http_status_code < 300:
        raise ValueError("execution receipt has no successful HTTP status")
    _timestamp(sanitized.get("completed_at"), "response completion timestamp")
    _canonical_source_urls(sanitized.get("source_urls"))
    if sanitized.get("request_id") is not None:
        _safe_text(sanitized.get("request_id"), "request_id", maximum=300)
    quick_scan_receipt_sha256(receipt)


def recovery_report(store: QuickScanWorkStore, *, run_id: str, scan_id: str) -> Dict[str, Any]:
    """Q07/JOB-04: read-only four-point crash partition for one run.

    Every item of the run lands in EXACTLY ONE bucket — no silent task loss:
    * ``pre_dispatch`` — pending (or leased without send intent): never sent,
      no cost, safe to re-dispatch;
    * ``unknown_in_flight`` — uncertain (or leased after send intent): the
      response is unknown — listed separately, never blindly re-POSTed;
    * ``persisted`` — has an answer checkpoint: never re-asked;
    * ``import_ack_pending`` — result_ready/delivered without checkpoint yet
      in the import/ACK flow (Q10's domain — annotated, re-import only);
    * ``cancelled`` — state change, history kept (JOB-05).
    """
    items = store.list_run_items(run_id, scan_id)
    report: Dict[str, Any] = {
        "pre_dispatch": [],
        "unknown_in_flight": [],
        "persisted": [],
        "import_ack_pending": [],
        "cancelled": [],
        "q10_import_ack_note": ("pre-ACK imports are Q10's domain: re-import only, never re-ask"),
    }
    for item in items:
        work_item_id = item["work_item_id"]
        try:
            checkpoint = store.get_answer_checkpoint(work_item_id)
        except Exception:  # noqa: BLE001
            checkpoint = None
        if checkpoint is not None:
            report["persisted"].append(work_item_id)
            continue
        status = item.get("status")
        if status == "pending":
            report["pre_dispatch"].append(work_item_id)
        elif status == "cancelled":
            report["cancelled"].append(work_item_id)
        elif status == "uncertain":
            report["unknown_in_flight"].append(work_item_id)
        elif status == "leased":
            attempts = store.list_attempts(work_item_id)
            sent = any(
                attempt.get("phase") not in {"prepared", "abandoned_unsent"} for attempt in attempts
            )
            bucket = "unknown_in_flight" if sent else "pre_dispatch"
            report[bucket].append(work_item_id)
        elif status in {"result_ready", "delivered"}:
            report["import_ack_pending"].append(work_item_id)
        else:
            report["unknown_in_flight"].append(work_item_id)
    return report


def cancel_pending_work(store: QuickScanWorkStore, work_item_ids: List[str]) -> Dict[str, str]:
    """Q07/JOB-05: cancel not-yet-run work by STATE CHANGE (history kept).

    Pending -> cancelled (idempotent); anything already running is refused
    by name — never silently cancelled, never deleted.
    """
    outcomes: Dict[str, str] = {}
    for work_item_id in work_item_ids:
        try:
            outcomes[work_item_id] = store.cancel_pending(work_item_id)
        except Exception as exc:  # noqa: BLE001
            outcomes[work_item_id] = f"refused:{type(exc).__name__}"
    return outcomes


def _spend_authorization_preflight(path: Optional[str]) -> Optional[str]:
    """B01-a/BENCH-02: validate the spend-authorization snapshot for a
    require-search run. Returns None when a valid snapshot authorizes the
    run; otherwise a BOUNDED reason code for the explicit
    needs_configuration result — before any attempt, reservation, or store
    file exists."""
    # r1 LOW-2: an absent flag falls back to the conventional default path
    # (cwd-relative), matching the card wording; absence still blocks.
    resolved = Path(path) if path else Path("spend_authorization.json")
    try:
        raw = resolved.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except OSError:
        return "spend_authorization_missing"
    except ValueError:
        return "spend_authorization_invalid"
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        return "spend_authorization_invalid"
    currency = payload.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        return "spend_authorization_invalid_currency"
    hard_cap = payload.get("hard_cap")
    if isinstance(hard_cap, bool) or not isinstance(hard_cap, (int, float)) or hard_cap <= 0:
        return "spend_authorization_invalid_hard_cap"
    snapshot_ref = payload.get("pricing_snapshot_ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
        return "spend_authorization_missing_pricing_snapshot"
    if not isinstance(payload.get("authorized_at"), str) or not payload["authorized_at"].strip():
        return "spend_authorization_invalid_authorized_at"
    return None


def load_run_c06_authority(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve the C06 delivery authority for one run (Q10/DB-07).

    An explicit ``--c06-authority`` path must load successfully — a malformed
    document is a configuration error and fails before any dispatch. Without
    the flag the conventional cwd-relative file is used when present; its
    absence returns ``None``, which the seal step turns into a durable block.
    """
    from src.utils.quick_scan_c06_authority import (
        DEFAULT_AUTHORITY_FILENAME,
        load_c06_authority,
    )

    if path is not None:
        return load_c06_authority(path)
    conventional = Path(DEFAULT_AUTHORITY_FILENAME)
    if conventional.is_file():
        return load_c06_authority(conventional)
    return None


def load_stock_list(file_path: str) -> list[str]:
    """从文件加载股票列表。

    Args:
        file_path: 股票列表文件路径

    Returns:
        股票名称列表

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件为空或格式错误
    """
    logger.info("正在加载股票列表: %s", file_path)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            stocks = [line.strip() for line in f if line.strip()]

        if not stocks:
            raise ValueError(f"股票列表文件为空: {file_path}")

        logger.info(f"成功加载 {len(stocks)} 个股票: {', '.join(stocks)}")
        return stocks

    except FileNotFoundError:
        logger.error("股票列表文件不存在: %s", file_path)
        raise
    except OSError as e:
        logger.error("加载股票列表失败: %s", e)
        raise


def process_batch_stocks(
    stocks: List[str],
    config_file: str,
    provider_name: Optional[str],
    output_dir: str,
    max_retries: int = 3,
    override: bool = False,
    config_format: str = "json",
) -> Dict[str, Any]:
    """批量处理多个股票（问题级别override）。

    Args:
        stocks: 股票名称列表
        config_file: 问题配置文件路径
        provider_name: LLM 提供商名称
        output_dir: 输出目录路径
        max_retries: 每个股票的最大重试次数
        override: 是否覆盖已存在的问题答案（默认：False，跳过已存在的问题）
        config_format: 配置文件格式（json 或 txt，默认：json）

    Returns:
        处理结果统计字典
    """
    import time

    start_time = time.time()
    results: Dict[str, Any] = {
        "total_stocks": len(stocks),
        "success_count": 0,
        "failed_count": 0,
        "failed_stocks": [],
        "output_files": [],
        "skipped_count": 0,
    }

    logger.info(f"\n{'=' * 70}")
    logger.info(f"开始批量处理 {len(stocks)} 个股票")
    logger.info(f"{'=' * 70}\n")

    # 加载问题（所有股票共享）
    questions = load_questions(config_file, config_format)

    # 依次处理每个股票
    for idx, stock in enumerate(stocks, 1):
        logger.info(f"\n{'=' * 70}")
        logger.info(f"[{idx}/{len(stocks)}] 正在处理: {stock}")
        logger.info(f"{'=' * 70}")

        # 检查输出文件是否已存在，读取现有答案
        output_filename = f"QALLM_{stock}.json"
        output_path = Path(output_dir) / output_filename
        existing_answers = load_existing_answers(output_path)

        # 计算需要处理的问题
        questions_to_process, skipped_questions, needs_processing = calculate_questions_to_process(
            questions, existing_answers, override
        )

        if not needs_processing:
            results["skipped_count"] = cast(int, results.get("skipped_count", 0)) + 1
            # 验证现有文件
            if provider_name is not None:
                validate_and_repair_existing_file(output_path, questions, stock, provider_name)
            continue

        # 处理股票（带重试）
        if provider_name is None:
            logger.error("provider_name is None, skipping stock")
            results["failed_count"] += 1
            results["failed_stocks"].append({"stock": stock, "error": "provider_name is None"})
            continue

        success, error = process_single_stock_with_retry(
            stock=stock,
            questions_to_process=questions_to_process,
            provider_name=provider_name,
            output_path=output_path,
            existing_answers=existing_answers,
            override=override,
            max_retries=max_retries,
            all_questions=questions,
        )

        if success:
            results["success_count"] = cast(int, results.get("success_count", 0)) + 1
            cast(List[str], results.get("output_files", [])).append(str(output_path))
        else:
            results["failed_count"] = cast(int, results.get("failed_count", 0)) + 1
            cast(List[Dict[str, Any]], results.get("failed_stocks", [])).append(
                {"stock": stock, "error": str(error) if error else "Unknown error"}
            )

    # 显示最终统计
    log_final_results(results, start_time, output_dir)
    return results


def log_final_results(results: dict[str, Any], start_time: float, output_dir: str) -> None:
    """记录最终结果统计。

    Args:
        results: 结果字典
        start_time: 开始时间
        output_dir: 输出目录
    """
    import time

    elapsed_time = time.time() - start_time
    logger.info(f"\n{'=' * 70}")
    logger.info("批量处理完成")
    logger.info(f"{'=' * 70}")
    logger.info(f"总股票数: {results['total_stocks']}")
    logger.info(f"成功处理: {results['success_count']}")
    logger.info(f"处理失败: {results['failed_count']}")
    if "skipped_count" in results:
        logger.info(f"已跳过: {results['skipped_count']}")
    logger.info("总耗时: %.1f 秒", elapsed_time)

    if results["failed_stocks"]:
        logger.warning("\n失败的股票:")
        for item in results["failed_stocks"]:
            logger.warning(f"  - {item['stock']}: {item['error']}")

    logger.info("\n输出文件位置: %s", output_dir)


class LLMRunner:
    """LLM运行器类。

    负责执行LLM模式的处理逻辑。
    """

    def __init__(self, verbose: bool = False):
        """初始化LLM运行器。

        Args:
            verbose: 是否启用详细日志输出
        """
        self.logger = get_logger(__name__, verbose=verbose)

    @staticmethod
    def _read_policy_effective_mode(config_path: Path) -> str:
        """Read the StockQA-owned effective-mode hint from a separate config key.

        ``next_run`` remains the default for any absent or unknown value; any
        value that is not ``immediate`` keeps runs on the safer default.
        """
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return "next_run"
        value = raw.get("quick_scan_model_policy_effective_mode")
        return value if isinstance(value, str) and value == "immediate" else "next_run"

    @staticmethod
    def _build_route_providers(
        policy_snapshot: Dict[str, Any],
        *,
        company: str,
        entity_id: Optional[str],
        config_file: Path,
    ) -> List[Optional[LLMProvider]]:
        """Materialize one provider instance per eligible ordered route."""
        route_providers: List[Optional[LLMProvider]] = []
        for route in policy_snapshot["routes"]:
            if not route["eligible"]:
                route_providers.append(None)
                continue
            route_provider = LLMProvider(
                provider_name=route["provider_config_ref"],
                model=route["model"],
                company_name=company,
                config_file=str(config_file),
                require_search=True,
                entity_id=entity_id,
            )
            # A quota-rejected route should advance promptly instead
            # of retrying the same exhausted account. Later quota
            # policy work can add persisted, category-specific waits.
            route_provider.max_retries = 1
            route_provider.retry_strategy.max_retries = 1
            # A separate repair request has no independent budget in
            # model-policy v2; keep it from exceeding the dispatch cap.
            route_provider.format_repair_budget = 0
            route_providers.append(route_provider)
        return route_providers

    def run(
        self,
        company: Optional[str] = None,
        batch_file: Optional[str] = None,
        provider: Optional[str] = None,
        config: str = "config.json",
        output: str = "outputs/analysis_result.json",
        override: bool = False,
        config_format: str = "json",
        entity_id: Optional[str] = None,
        require_search: bool = False,
        identity_snapshot: Optional[str] = None,
        spend_authorization: Optional[str] = None,
        c06_authority: Optional[str] = None,
        seal_deliveries: bool = False,
        question_manifest: Optional[str] = None,
        security_scope_id: Optional[str] = None,
        search_policy: Optional[str] = None,
    ) -> int:
        """运行LLM模式处理。

        Args:
            company: 公司名称（单股票模式）
            batch_file: 批量处理文件路径（批量模式）
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            override: 是否覆盖已存在的文件
            config_format: 配置文件格式
            identity_snapshot: 可选；W04 ``identity-export-g2b`` 身份包 JSON
                路径——提供后逐题 work-item 生命周期激活（Q06），缺省关闭。
            c06_authority: 可选；版本化 C06 权威文档路径（Q10）。显式提供
                却无效时在任何 HTTP 之前失败关闭；缺省时按约定路径查找，
                找不到则检查点落 durable block（DB-07），不猜补字段。
            seal_deliveries: 重启/补封入口——只把已落定的检查点封存或记录
                阻断，模型请求恒为 0，不需要 --company/--batch。

        Returns:
            退出码（0 表示成功，1 表示失败）
        """
        # 验证参数互斥性
        if seal_deliveries:
            if company or batch_file:
                raise ValueError("--seal-deliveries 不能与 company/batch 同时使用")
            return self._run_seal_deliveries(c06_authority=c06_authority)

        if company and batch_file:
            raise ValueError("不能同时使用 company 和 batch_file 参数")

        if not company and not batch_file:
            raise ValueError("必须指定 company 或 batch_file 参数")
        if require_search and batch_file:
            raise ValueError(
                "--require-search 当前只支持单公司入口；批量接续由quick-scan运行器负责"
            )
        if require_search and not entity_id:
            raise ValueError("--require-search必须显式提供 --entity-id，不能从公司名称猜测")
        if identity_snapshot and not require_search:
            raise ValueError("--identity-snapshot 需要与 --require-search 同时使用")
        if question_manifest is not None and not require_search:
            raise ValueError("--question-manifest 需要与 --require-search 同时使用")
        if question_manifest is not None and not identity_snapshot:
            raise ValueError(
                "--question-manifest 需要 --identity-snapshot：增量派发计划必须"
                "对账已有 work item，不能只凭题面重问"
            )
        if security_scope_id is not None and question_manifest is None:
            raise ValueError("--security-scope-id 只在 --question-manifest 下生效")
        if search_policy is not None and not require_search:
            raise ValueError("--search-policy 需要与 --require-search 同时使用")
        # B01-a/BENCH-02: the public quick-scan entry fails closed on
        # zero/unknown spend authorization BEFORE creating any dispatchable
        # attempt — an explicit bounded blocked result, a fresh auditable run
        # id, zero outbound requests, and zero budget/work rows.
        if require_search:
            blocked = _spend_authorization_preflight(spend_authorization)
            if blocked is not None:
                payload = {
                    "schema": "stockqa.spend_preflight/1.0.0",
                    "status": "needs_configuration",
                    "blocked": True,
                    "reason": blocked,
                    "run_id": "run-"
                    + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                    + "-"
                    + uuid.uuid4().hex[:6],
                }
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 2
        identity_payload = (
            load_identity_snapshot(identity_snapshot, expected_entity_id=entity_id)
            if identity_snapshot
            else None
        )

        self.logger.info("=" * 70)
        if company:
            self.logger.info("公司分析 - %s", company)
            self.logger.info("=" * 70)
            return self._run_single_company(
                company=company,
                provider=provider,
                config=config,
                output=output,
                config_format=config_format,
                entity_id=entity_id,
                require_search=require_search,
                identity_payload=identity_payload,
                c06_authority_path=c06_authority,
                question_manifest=question_manifest,
                security_scope_id=security_scope_id,
                search_policy=search_policy,
            )
        else:
            self.logger.info("批量股票分析模式")
            self.logger.info("=" * 70)
            # batch_file is guaranteed to be non-None here due to the validation above
            return self._run_batch_mode(
                batch_file=cast(str, batch_file),
                provider=provider,
                config=config,
                output=output,
                override=override,
                config_format=config_format,
            )

    def _run_seal_deliveries(self, *, c06_authority: Optional[str]) -> int:
        """Q10 public recovery entry: settle checkpoints into C06 packages.

        Zero model requests, zero HTTP — it only reads the durable store,
        seals what the authority now allows, and records blocks for the rest.
        """
        from src.utils.quick_scan_delivery_seal import seal_pending_deliveries

        authority = load_run_c06_authority(c06_authority)
        llm_config = LLMConfig("llm_apis.json")
        store = QuickScanWorkStore(llm_config.config_file.parent / "quick_scan_work.sqlite")
        report = seal_pending_deliveries(store, authority=authority)
        payload = {
            "schema": "stockqa.seal_deliveries/1.0.0",
            "authority_sha256": None if authority is None else authority["authority_sha256"],
            "store": str(store.path),
            **report,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0

    def _run_single_company(
        self,
        company: str,
        provider: Optional[str],
        config: str,
        output: str,
        config_format: str,
        entity_id: Optional[str] = None,
        require_search: bool = False,
        identity_payload: Optional[Dict[str, Any]] = None,
        c06_authority_path: Optional[str] = None,
        question_manifest: Optional[str] = None,
        security_scope_id: Optional[str] = None,
        search_policy: Optional[str] = None,
    ) -> int:
        """运行单公司处理模式。

        Args:
            company: 公司名称
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            config_format: 配置文件格式

        Returns:
            退出码
        """
        try:
            # Bound on every path below before first read, so no branch can
            # observe an unbound name (explicit over implicit control flow).
            c06_authority: Optional[Dict[str, Any]] = None
            if require_search and config_format != "json":
                raise ValueError("--require-search要求JSON题目文件中的显式question_id")
            config_manager: ConfigProvider
            if config_format == "json":
                config_manager = JSONConfigManager(config)
            else:
                config_manager = ConfigManager(config)
            questions = (
                config_manager.load_question_items()
                if require_search and isinstance(config_manager, JSONConfigManager)
                else config_manager.load_questions()
            )
            if require_search and any(
                not isinstance(question, Question) or not question.question_id
                for question in questions
            ):
                raise ValueError("--require-search要求每道题提供稳定question_id")

            # Q13: the frozen questionnaire manifest is consumed and BOUND to
            # the loaded question file before any attempt, reservation or HTTP.
            # Duplicate IDs, truncation, re-worded prompts or a cross-module
            # replacement conflict are refused here with a bounded reason.
            manifest: Optional[Dict[str, Any]] = None
            if question_manifest is not None:
                from src.utils.quick_scan_question_manifest import (
                    bind_manifest_to_questions,
                    load_question_manifest,
                )

                manifest = load_question_manifest(question_manifest)
                bind_manifest_to_questions(manifest, cast(List[Question], questions))
                self.logger.info(
                    "冻结问卷已校验（%s 题，manifest %s）",
                    manifest["question_count"],
                    manifest["manifest_sha256"][:16],
                )

            # Q02: import and admit the versioned search policy BEFORE any
            # attempt. The IQS fillable template is refused as non-executable,
            # and an external mode with no admitted route fails closed instead
            # of silently falling back to native search.
            if search_policy is not None:
                from src.config.quick_scan_search_policy import (
                    load_search_policy,
                    policy_receipt,
                )

                search_document = load_search_policy(search_policy)
                print(
                    json.dumps(
                        policy_receipt(search_document),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                if search_document.requires_external and not search_document.admitted:
                    rejections = ",".join(
                        f"{item['route_id']}:{item['reason']}"
                        for item in search_document.rejections
                    )
                    raise ValueError("external_search_routes_unadmitted: " + rejections)
                if search_document.requires_external:
                    raise ValueError(
                        "external_context_not_implemented: admitted routes have no production dispatcher"
                    )

            # Q10/DB-07: resolve the C06 delivery authority BEFORE any HTTP.
            # An explicitly supplied but invalid document fails the run closed;
            # an absent conventional document means every checkpoint settles as
            # a durable block instead of an invented envelope field.
            c06_authority = load_run_c06_authority(c06_authority_path)
            if c06_authority is not None:
                self.logger.info("C06 权威文档已加载（%s）", c06_authority["authority_sha256"][:16])
            elif c06_authority_path is None:
                self.logger.warning(
                    "未找到 C06 权威文档：检查点将记录 durable block，稍后由 "
                    "--seal-deliveries 补封（模型请求不增加）"
                )

            self.logger.info(f"\n成功加载 {len(questions)} 个问题")
            self.logger.info(f"配置文件: {config} ({config_format}格式)")
            self.logger.info("输出文件: %s\n", output)

            # Quick-scan snapshots StockQA's ordered model policy at run start.
            # With the default effective mode the snapshot applies to the whole
            # run; explicit ``immediate`` re-reads the policy at each
            # undispatched question boundary and records revision transitions.
            quick_scan_policy = None
            budget_store = None
            cost_resolver = None
            llm_config: Optional[LLMConfig] = None
            if provider is None and not require_search:
                raise ValueError("provider 参数不能为 None")
            if require_search:
                llm_config = LLMConfig("llm_apis.json")
                policy = llm_config.get_quick_scan_model_policy(default_provider=provider)
                quick_scan_policy = policy
                if policy["configured"]:
                    cost_resolver = QuickScanCostResolver(
                        llm_config.config_file.parent / "quick_scan_rate_cards.json",
                        policy,
                    )
                    if (
                        policy["cost_policy"]["pricing_basis"] == "verified_rate_card"
                        and not cost_resolver.has_pricing_reference
                    ):
                        self.logger.warning(
                            "Quick-scan rate card %s is unavailable; after the first provider "
                            "attempt its reserved cost will remain unpriced and dispatch will pause.",
                            policy["cost_policy"]["pricing_ref"],
                        )
                    budget_store = QuickScanWorkStore(
                        llm_config.config_file.parent / "quick_scan_work.sqlite"
                    )
                    route_providers: List[Optional[LLMProvider]] = []
                    for route in policy["routes"]:
                        if not route["eligible"]:
                            route_providers.append(None)
                            continue
                        route_provider = LLMProvider(
                            provider_name=route["provider_config_ref"],
                            model=route["model"],
                            company_name=company,
                            config_file=str(llm_config.config_file),
                            require_search=True,
                            entity_id=entity_id,
                        )
                        # A quota-rejected route should advance promptly instead
                        # of retrying the same exhausted account. Later quota
                        # policy work can add persisted, category-specific waits.
                        route_provider.max_retries = 1
                        route_provider.retry_strategy.max_retries = 1
                        # A separate repair request has no independent budget in
                        # model-policy v2; keep it from exceeding the dispatch cap.
                        route_provider.format_repair_budget = 0
                        route_providers.append(route_provider)

                    def policy_provider() -> Dict[str, Any]:
                        try:
                            fresh = LLMConfig(str(llm_config.config_file))
                            snapshot = fresh.get_quick_scan_model_policy(default_provider=provider)
                            if snapshot.get("configured"):
                                return snapshot
                        except Exception:
                            self.logger.exception(
                                "Quick-scan hot policy reload failed; keeping the "
                                "running revision."
                            )
                        return quick_scan_policy

                    llm_provider: SearchProvider = OrderedSearchProviderCascade(
                        providers=route_providers,
                        routes=policy["routes"],
                        policy_version=policy["policy_version"],
                        max_attempts_per_dispatch_round=policy["max_attempts_per_dispatch_round"],
                        require_search=True,
                        health_store=QuickScanProviderHealth(
                            llm_config.config_file.parent / "quick_scan_health.sqlite"
                        ),
                        quota_groups=policy["quota_groups"],
                        policy_source={
                            "policy_provider": policy_provider,
                            "provider_factory": lambda snapshot: self._build_route_providers(
                                snapshot,
                                company=company,
                                entity_id=entity_id,
                                config_file=llm_config.config_file,
                            ),
                            "effective_mode": lambda: self._read_policy_effective_mode(
                                llm_config.config_file
                            ),
                        },
                    )
                else:
                    # Preserve existing retry behavior until the user activates
                    # an ordered multi-model policy.
                    llm_provider = LLMProvider(
                        provider_name=policy["routes"][0]["provider_config_ref"],
                        model=policy["routes"][0]["model"] or None,
                        company_name=company,
                        config_file=str(llm_config.config_file),
                        require_search=True,
                        entity_id=entity_id,
                    )
            else:
                llm_provider = LLMProvider(
                    provider_name=provider,
                    company_name=company,
                    require_search=False,
                    entity_id=entity_id,
                )

            answer_generator = AnswerGenerator()
            work_lifecycle = None
            if identity_payload is not None:
                if not entity_id:
                    raise ValueError("--identity-snapshot 需要 --entity-id")
                if llm_config is None:
                    # --identity-snapshot requires --require-search, which is
                    # the only branch that binds the quick-scan config; keep the
                    # dependency explicit instead of relying on branch order.
                    raise RuntimeError("quick-scan lifecycle binding: llm config is not bound")
                work_store = budget_store or QuickScanWorkStore(
                    llm_config.config_file.parent / "quick_scan_work.sqlite"
                )
                # Q13: the incremental plan over the frozen manifest is a
                # read-only receipt printed BEFORE dispatch; its per-question
                # scope/generation bindings are the only authority the
                # lifecycle uses to attach work items.
                scope_bindings: Dict[str, Dict[str, str]] = {}
                generation_overrides: Dict[str, int] = {}
                routing_overrides: Dict[str, str] = {}
                if manifest is not None:
                    from src.utils.quick_scan_question_manifest import (
                        manifest_scope_bindings,
                        plan_manifest_dispatch,
                    )

                    frozen_entity = entity_id
                    scope_bindings = manifest_scope_bindings(
                        manifest,
                        entity_id=frozen_entity,
                        security_scope_id=security_scope_id,
                    )
                    dispatch_plan = plan_manifest_dispatch(
                        manifest,
                        work_store,
                        entity_id=frozen_entity,
                        identity_snapshot_sha256=identity_payload["identity_snapshot_sha256"],
                        bindings=scope_bindings,
                    )
                    generation_overrides = dispatch_plan["generation_by_question"]
                    routing_overrides = dispatch_plan["routing_fingerprint_by_question"]
                    print(json.dumps(dispatch_plan, ensure_ascii=False, sort_keys=True))
                # Q09: reserve-before-dispatch rides the activated lifecycle —
                # when a policy is configured, the pair reserves against the
                # FIRST ELIGIBLE route (the cascade may fall back afterwards;
                # total-ledger conservation is unaffected, route attribution
                # is the intended route — disclosed on the Q09 card).
                budget_pair_policy = None
                budget_pair_route = None
                primary_route = None
                if quick_scan_policy is not None and quick_scan_policy.get("configured"):
                    primary_route = next(
                        (r for r in quick_scan_policy.get("routes", []) if r.get("eligible")),
                        None,
                    )
                    if primary_route is not None:
                        budget_pair_policy = quick_scan_policy
                        budget_pair_route = {
                            "route_id": primary_route["id"],
                            "provider": primary_route["provider_config_ref"],
                            "model_requested": primary_route["model"],
                            "quota_group": primary_route["quota_group"],
                        }
                # Q10/Q07 glue: the attempt's ``model_requested`` must name the
                # model that actually answers, otherwise the checkpoint contract
                # (attempt.model_requested == receipt.actual_model) can never
                # hold and NO checkpoint — hence no C06 package — would ever
                # land on the production activation path.
                if primary_route is not None:
                    lifecycle_model = primary_route["model"]
                elif quick_scan_policy is not None and quick_scan_policy.get("routes"):
                    lifecycle_model = quick_scan_policy["routes"][0]["model"]
                else:
                    lifecycle_model = None
                work_lifecycle = QuickScanWorkLifecycle(
                    work_store,
                    entity_id=entity_id,
                    run_id="run-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                    scan_id="scan-l02",
                    identity=identity_payload,
                    routing_fingerprint=routing_fingerprint_for(questions),
                    budget_policy=budget_pair_policy,
                    budget_route=budget_pair_route,
                    model_requested=lifecycle_model or "quick-scan",
                    route_id=(
                        primary_route or (quick_scan_policy or {}).get("routes", [{}])[0]
                    ).get("id", "cli"),
                    provider_name=(
                        primary_route or (quick_scan_policy or {}).get("routes", [{}])[0]
                    ).get("provider_config_ref", "quick-scan-cli"),
                    c06_authority=c06_authority,
                    scope_by_question=scope_bindings,
                    generation_by_question=generation_overrides,
                    routing_fingerprint_by_question=routing_overrides,
                    transport_managed=True,
                )
            qa_engine = QAEngine(llm_provider, answer_generator, work_item_lifecycle=work_lifecycle)

            # 处理问题
            if quick_scan_policy is not None and quick_scan_policy["configured"]:
                if budget_store is None:
                    raise RuntimeError("quick-scan budget binding: budget store is not bound")
                with bind_quick_scan_budget(
                    budget_store, quick_scan_policy, cost_resolver=cost_resolver
                ):
                    batch_result = qa_engine.process_questions(questions)
            else:
                batch_result = qa_engine.process_questions(questions)

            # 显示结果摘要
            print("\n" + "=" * 70)
            if company:
                print(f"{company} 分析报告")
            else:
                print("分析报告")
            print("=" * 70)

            for i, result in enumerate(batch_result.results, 1):
                question = result.question.text
                # 只显示问题前60个字符，避免太长
                display_question = question if len(question) <= 60 else question[:60] + "..."

                print(f"\n{'─' * 70}")
                print(f"[{i}/{len(questions)}] {display_question}")
                print(f"{'─' * 70}")
                score_text = f"{result.answer.score}/10" if result.answer.score is not None else "—"
                print(f"Score: {score_text}")
                # 显示描述的前200个字符
                description_preview = (
                    result.answer.text[:200] + "..."
                    if len(result.answer.text) > 200
                    else result.answer.text
                )
                print(f"\n{description_preview}")

            # 输出完整结果到JSON文件
            if require_search and quick_scan_policy is not None:
                qa_engine.output_results(
                    batch_result,
                    output,
                    quick_scan_context={
                        "entity_id": cast(str, entity_id),
                        "company_name": company,
                        # Top-level fields show the configured preference; the
                        # per-question receipt names the route that answered.
                        "provider_name": quick_scan_policy["routes"][0]["provider_config_ref"],
                        "requested_model": quick_scan_policy["routes"][0]["model"],
                    },
                )
            elif require_search:
                raise RuntimeError("quick-scan output binding: model policy is not bound")
            else:
                qa_engine.output_results(batch_result, output)

            # 显示统计
            stats = qa_engine.get_statistics(batch_result)
            print("\n" + "=" * 70)
            print("分析完成统计")
            print("=" * 70)
            print(f"[OK] 处理问题数: {stats['total_questions']}")
            print(f"[OK] 成功处理: {stats['success_count']}")
            if require_search:
                unscored_count = sum(
                    1 for item in batch_result.results if item.answer.score is None
                )
                print(f"[INFO] 未评分问题: {unscored_count}")
            else:
                print("[OK] 处理成功率: 100%")
            print(f"[OK] 完整报告已保存到: {output}")

            # 计算平均评分
            scored_answers = [
                r.answer.score for r in batch_result.results if r.answer.score is not None
            ]
            if scored_answers:
                avg_score = sum(scored_answers) / len(scored_answers)
                print(f"[OK] 平均评分: {avg_score:.1f}/10")

                # 显示评分分布
                score_distribution: Dict[int, int] = {}
                for r in batch_result.results:
                    score = r.answer.score
                    if score is not None:
                        score_distribution[score] = score_distribution.get(score, 0) + 1
                print(f"[OK] 评分分布: {dict(sorted(score_distribution.items()))}")

            if require_search and stats["error_count"]:
                self.logger.error(
                    "Quick-scan contains %d failed question(s); partial results were saved.",
                    stats["error_count"],
                )
                return 1

            self.logger.info("\nQuestion processing completed.")
            return 0

        except (ConnectionError, TimeoutError, OSError) as e:
            self.logger.error(f"处理失败: {e}", exc_info=True)
            return 1

    def _run_batch_mode(
        self,
        batch_file: str,
        provider: Optional[str],
        config: str,
        output: str,
        override: bool,
        config_format: str,
    ) -> int:
        """运行批量处理模式。

        Args:
            batch_file: 批量处理文件路径
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            override: 是否覆盖已存在的文件
            config_format: 配置文件格式

        Returns:
            退出码
        """
        try:
            # 加载股票列表
            stocks = load_stock_list(batch_file)

            # 确保输出目录存在
            output_dir = Path(output).parent
            output_dir.mkdir(parents=True, exist_ok=True)

            # 批量处理
            results = process_batch_stocks(
                stocks=stocks,
                config_file=config,
                provider_name=provider,
                output_dir=str(output_dir),
                override=override,
                config_format=config_format,
            )

            # 返回码：如果有失败则返回 1
            return 1 if results["failed_count"] > 0 else 0

        except (ValueError, TypeError, OSError, ConnectionError, TimeoutError) as e:
            self.logger.error(f"批量处理失败: {e}", exc_info=True)
            return 1


def main() -> int:
    """LLM运行器命令行入口。

    用于独立运行LLM模式。

    Returns:
        退出码（0 表示成功，1 表示失败）
    """
    parser = argparse.ArgumentParser(
        description="使用LLM分析公司股票投资价值",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 分析海康威视（使用默认配置文件中的 API，JSON格式）
  python -m src.runners.llm_runner --company "海康威视"

  # 使用文本配置文件
  python -m src.runners.llm_runner --company "海康威视" --config config.txt --config-format txt

  # 分析其他公司
  python -m src.runners.llm_runner --company "腾讯控股" --config questions.txt \\
         --config-format txt --output tencent_analysis.json

  # 指定使用特定的 LLM 提供商
  python -m src.runners.llm_runner --company "阿里巴巴" --provider minimax

  # 批量处理股票列表（跳过已存在的输出文件，默认JSON配置）
  python -m src.runners.llm_runner --batch input_stocks.txt

  # 批量处理并覆盖已存在的输出文件，使用文本配置
  python -m src.runners.llm_runner --batch input_stocks.txt --override --config-format txt

配置文件：
  API 密钥请在 llm_apis.json 中配置
        """,
    )

    parser.add_argument(
        "--company", "-c", type=str, help="要分析的公司名称（如：海康威视、腾讯控股等）"
    )

    parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default=None,
        help="指定使用的 LLM 提供商（如：deepseek, minimax, glm），默认使用配置文件中的默认提供商",
    )

    parser.add_argument(
        "--config",
        "-f",
        type=str,
        default="config.json",
        help="问题配置文件路径（默认：config.json）",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="outputs/analysis_result.json",
        help="输出JSON文件路径（默认：outputs/analysis_result.json）",
    )

    parser.add_argument(
        "--batch",
        "-b",
        type=str,
        metavar="FILE",
        help="批量处理模式：从文件读取股票列表（每行一个股票名称）",
    )

    parser.add_argument(
        "--override",
        action="store_true",
        help="覆盖已存在的输出文件（默认：跳过已存在的文件）",
    )

    parser.add_argument(
        "--config-format",
        type=str,
        choices=["json", "txt"],
        default="json",
        help="指定配置文件格式（json 或 txt，默认：json）",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="启用详细日志输出")

    args = parser.parse_args()

    runner = LLMRunner(verbose=args.verbose)
    return runner.run(
        company=args.company,
        batch_file=args.batch,
        provider=args.provider,
        config=args.config,
        output=args.output,
        override=args.override,
        config_format=args.config_format,
    )


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
