"""Q13: consume the frozen IQS questionnaire manifest and plan incrementally.

IQS owns the manifest (question IDs, module/semantic/prompt hashes, scope).
StockQA only VALIDATES the frozen bindings it dispatches on and decides —
per question and before any HTTP — whether the answer still needs a model
call. Tampering, duplicate IDs, truncation or a cross-module replacement
conflict is refused with a bounded, auditable reason code.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.core.models import Question
from src.utils.quick_scan_work_store import QuickScanWorkStore

__all__ = [
    "MANIFEST_CONTRACT_ID",
    "QuestionManifestRejected",
    "bind_manifest_to_questions",
    "load_question_manifest",
    "manifest_scope_bindings",
    "plan_manifest_dispatch",
]

MANIFEST_CONTRACT_ID = "stockqa.consumes_iqs_question_manifest/1.0.0"
PLAN_SCHEMA = "stockqa.question_manifest_plan/1.0.0"
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SCOPES = {"entity", "security", "segment"}
_REQUIRED_TOP = (
    "schema_version",
    "template_version",
    "question_count",
    "questions",
    "modules",
    "module_locks",
    "replacements",
)
_REQUIRED_QUESTION = (
    "id",
    "module_id",
    "scope",
    "prompt",
    "prompt_sha256",
    "semantic_sha256",
    "definition_sha256",
)


class QuestionManifestRejected(ValueError):
    """The frozen manifest (or its binding to the question file) is not
    dispatchable — refuse BEFORE any attempt, reservation or HTTP request."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_question_manifest(path: str | Path) -> dict[str, Any]:
    """Read and structurally validate one published questionnaire manifest."""
    resolved = Path(path)
    if resolved.is_dir():
        resolved = resolved / "manifest.json"
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise QuestionManifestRejected("manifest_unreadable", error.__class__.__name__) from error
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise QuestionManifestRejected("manifest_invalid_json", str(error)) from error
    if not isinstance(document, dict):
        raise QuestionManifestRejected("manifest_not_an_object")
    missing = [key for key in _REQUIRED_TOP if key not in document]
    if missing:
        raise QuestionManifestRejected("manifest_missing_fields", ",".join(missing))
    if not isinstance(document["schema_version"], str) or not document["schema_version"]:
        raise QuestionManifestRejected("manifest_missing_fields", "schema_version")

    questions = document["questions"]
    if not isinstance(questions, list) or not questions:
        raise QuestionManifestRejected("manifest_question_set_empty")
    count = document["question_count"]
    if type(count) is not int or count != len(questions):
        raise QuestionManifestRejected(
            "manifest_truncated", f"question_count={count!r} actual={len(questions)}"
        )
    modules = document["modules"]
    if (
        not isinstance(modules, list)
        or not modules
        or any(not isinstance(module_id, str) for module_id in modules)
    ):
        raise QuestionManifestRejected("manifest_missing_fields", "modules")
    module_set = set(modules)
    replacements = document["replacements"]
    if not isinstance(replacements, dict):
        raise QuestionManifestRejected("manifest_missing_fields", "replacements")

    seen: set[str] = set()
    replacement_targets: list[str] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise QuestionManifestRejected("manifest_malformed_question", str(index))
        absent = [key for key in _REQUIRED_QUESTION if key not in question]
        if absent:
            raise QuestionManifestRejected(
                "manifest_malformed_question", f"{index}:{','.join(absent)}"
            )
        question_id = question["id"]
        if not isinstance(question_id, str) or not _SAFE_ID.fullmatch(question_id):
            raise QuestionManifestRejected("manifest_malformed_question", f"{index}:id")
        if question_id in seen:
            raise QuestionManifestRejected("manifest_duplicate_question_id", question_id)
        seen.add(question_id)
        if question["module_id"] not in module_set:
            raise QuestionManifestRejected(
                "manifest_unknown_module", f"{question_id}:{question['module_id']}"
            )
        if question["scope"] not in _SCOPES:
            raise QuestionManifestRejected(
                "manifest_unsupported_scope", f"{question_id}:{question['scope']}"
            )
        prompt = question["prompt"]
        if not isinstance(prompt, str) or not prompt:
            raise QuestionManifestRejected("manifest_malformed_question", f"{question_id}:prompt")
        if _sha256_text(prompt) != question["prompt_sha256"]:
            raise QuestionManifestRejected("manifest_prompt_hash_mismatch", question_id)
        for key in ("semantic_sha256", "definition_sha256", "prompt_sha256"):
            value = question[key]
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise QuestionManifestRejected(
                    "manifest_malformed_question", f"{question_id}:{key}"
                )
        metric_contract = question.get("metric_contract")
        if isinstance(metric_contract, dict):
            target = metric_contract.get("replacement_for")
            if target is not None:
                if not isinstance(target, str):
                    raise QuestionManifestRejected(
                        "manifest_malformed_question", f"{question_id}:replacement_for"
                    )
                replacement_targets.append(target)

    if len(set(replacement_targets)) != len(replacement_targets):
        raise QuestionManifestRejected(
            "manifest_conflicting_replacement",
            ",".join(sorted({t for t in replacement_targets if replacement_targets.count(t) > 1})),
        )
    if set(replacements) & seen:
        raise QuestionManifestRejected(
            "manifest_conflicting_replacement",
            ",".join(sorted(set(replacements) & seen)),
        )
    listed_targets = list(replacements.values())
    if len(set(listed_targets)) != len(listed_targets):
        raise QuestionManifestRejected("manifest_conflicting_replacement", "replacements")

    return {
        **document,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_path": str(resolved),
        "contract_id": MANIFEST_CONTRACT_ID,
    }


def bind_manifest_to_questions(manifest: dict, questions: Sequence[Question]) -> None:
    """Bind the loaded question file to the frozen manifest (both directions).

    A question file edited after composition — different IDs, reordered or
    re-worded text — is refused before the runner builds any attempt.
    """
    expected = manifest["questions"]
    observed_ids = [
        question.question_id for question in questions if question.question_id is not None
    ]
    expected_ids = [question["id"] for question in expected]
    if len(observed_ids) != len(questions):
        raise QuestionManifestRejected("manifest_question_set_mismatch", "missing question_id")
    if observed_ids != expected_ids:
        raise QuestionManifestRejected(
            "manifest_question_set_mismatch",
            f"expected={len(expected_ids)} observed={len(observed_ids)}",
        )
    for question, frozen in zip(questions, expected):
        if _sha256_text(question.text) != frozen["prompt_sha256"]:
            raise QuestionManifestRejected("manifest_prompt_hash_mismatch", frozen["id"])


def manifest_scope_bindings(
    manifest: dict,
    *,
    entity_id: str,
    security_scope_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Map each frozen question to its dispatch scope.

    ``entity`` questions bind to the issuer; ``security``/``segment`` questions
    only bind when the caller supplies the authoritative scope ID — StockQA
    never invents a listing/segment identity from a company name.
    """
    bindings: dict[str, dict[str, str]] = {}
    unbound: list[str] = []
    for question in manifest["questions"]:
        scope = question["scope"]
        question_id = question["id"]
        if scope == "entity":
            bindings[question_id] = {"scope": "entity", "scope_id": entity_id}
        elif scope == "security" and security_scope_id:
            bindings[question_id] = {"scope": "security", "scope_id": security_scope_id}
        else:
            unbound.append(question_id)
    for question_id in unbound:
        bindings[question_id] = {"scope": "unbound", "scope_id": ""}
    return bindings


def _classify(
    rows: list[dict],
    *,
    fingerprint: str,
    scope: str,
    scope_id: str,
    identity_snapshot_sha256: str,
    store: QuickScanWorkStore,
) -> tuple[str, int, str | None]:
    """Classify one manifest question against its stored work items."""
    if not rows:
        return "dispatch", 1, None
    compatible = [
        row
        for row in rows
        if row["scope"] == scope
        and row["scope_id"] == scope_id
        and row["identity_snapshot_sha256"] == identity_snapshot_sha256
    ]
    if not compatible:
        # a different frozen scope/identity is a different logical item
        return "dispatch", 1, None
    matching = [row for row in compatible if row["question_fingerprint"] == fingerprint]
    if not matching:
        # the frozen prompt changed: the stored answer is expired for this
        # manifest — start a NEW generation, never rebind the frozen inputs.
        return (
            "expired_dispatch",
            max(row["generation"] for row in compatible) + 1,
            None,
        )
    current = max(matching, key=lambda row: row["generation"])
    status = current["status"]
    work_item_id = current["work_item_id"]
    if status == "pending":
        return "dispatch", current["generation"], work_item_id
    if status == "leased":
        return "in_flight", current["generation"], work_item_id
    if status == "uncertain":
        return "reconcile", current["generation"], work_item_id
    if status == "cancelled":
        return "skip_cancelled", current["generation"], work_item_id

    checkpoint = store.get_answer_checkpoint(work_item_id)
    if checkpoint is None:
        return "dispatch", current["generation"], work_item_id
    answer_status = checkpoint["payload"]["answer"].get("status")
    if answer_status == "not_applicable":
        return "skip_not_applicable", current["generation"], work_item_id
    delivery = store.get_result_delivery(work_item_id)
    if status == "result_ready" and (delivery is None or delivery.get("package") is None):
        return "seal_only", current["generation"], work_item_id
    return "reuse", current["generation"], work_item_id


def plan_manifest_dispatch(
    manifest: dict,
    store: QuickScanWorkStore,
    *,
    entity_id: str,
    identity_snapshot_sha256: str,
    bindings: dict[str, dict[str, str]],
    base_generation: int = 1,
) -> dict[str, Any]:
    """Produce the auditable per-question dispatch plan for one entity.

    Actions: ``dispatch`` / ``expired_dispatch`` are the only ones that may
    consume a model call; everything else is reuse, reconciliation, sealing or
    an explicit bounded deferral.
    """
    if type(base_generation) is not int or base_generation < 1:
        raise ValueError("base_generation must be >= 1")
    questions = manifest["questions"]
    rows_by_id = store.find_work_items(
        entity_id=entity_id, question_ids=[question["id"] for question in questions]
    )
    plan: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    generation_by_question: dict[str, int] = {}
    for question in questions:
        question_id = question["id"]
        binding = bindings.get(question_id) or {"scope": "unbound", "scope_id": ""}
        # the work store fingerprints the STRIPPED question text, while the
        # frozen prompt hash covers the published bytes — both are checked.
        work_fingerprint = _sha256_text(question["prompt"].strip())
        if binding["scope"] == "unbound":
            action, generation, work_item_id = (
                "deferred_scope_unbound",
                base_generation,
                None,
            )
        else:
            action, generation, work_item_id = _classify(
                rows_by_id.get(question_id, []),
                fingerprint=work_fingerprint,
                scope=binding["scope"],
                scope_id=binding["scope_id"],
                identity_snapshot_sha256=identity_snapshot_sha256,
                store=store,
            )
            if action == "dispatch" and work_item_id is None:
                generation = base_generation
        counts[action] = counts.get(action, 0) + 1
        if action == "expired_dispatch":
            generation_by_question[question_id] = generation
        plan.append(
            {
                "question_id": question_id,
                "module_id": question["module_id"],
                "scope": binding["scope"],
                "scope_id": binding["scope_id"],
                "semantic_sha256": question["semantic_sha256"],
                "prompt_sha256": question["prompt_sha256"],
                "action": action,
                "generation": generation,
                "work_item_id": work_item_id,
            }
        )
    model_calls_planned = counts.get("dispatch", 0) + counts.get("expired_dispatch", 0)
    return {
        "schema": PLAN_SCHEMA,
        "contract_id": MANIFEST_CONTRACT_ID,
        "manifest_sha256": manifest["manifest_sha256"],
        "template_version": manifest.get("template_version"),
        "module_package_id": manifest.get("module_package_id"),
        "entity_id": entity_id,
        "identity_snapshot_sha256": identity_snapshot_sha256,
        "counts": dict(sorted(counts.items())),
        "model_calls_planned": model_calls_planned,
        "generation_by_question": dict(sorted(generation_by_question.items())),
        "questions": plan,
    }


def plan_json_lines(plan: dict[str, Any]) -> Iterable[str]:
    """Single-line JSON rendering used by the public CLI receipt."""
    yield json.dumps(plan, ensure_ascii=False, sort_keys=True)
