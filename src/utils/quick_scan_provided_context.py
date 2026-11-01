"""Short sources actually sent by the original research owner; never search proof."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf8")).hexdigest()


_FIELDS = (
    "entity_id",
    "question_id",
    "generation",
    "scope",
    "scope_id",
    "identity_revision",
    "identity_snapshot_sha256",
)
_SOURCE_FIELDS = (
    "evidence_id",
    "source_url",
    "published_at",
    "period_start",
    "period_end",
    "excerpt_sha256",
)


def _sources(records, cutoff):
    from .quick_scan_work_store import _canonical_source_urls, _sha256

    boundary = date.fromisoformat(cutoff)
    result = []
    ids = set()
    for record in records:
        if not isinstance(record, dict) or not set(_SOURCE_FIELDS) <= set(record):
            raise ValueError("provided_context_source_invalid")
        if (
            not isinstance(record["evidence_id"], str)
            or not record["evidence_id"]
            or record["evidence_id"] in ids
        ):
            raise ValueError("provided_context_evidence_id_invalid")
        ids.add(record["evidence_id"])
        _sha256(record["excerpt_sha256"], "provided excerpt hash")
        _canonical_source_urls([record["source_url"]])
        for field in ("published_at", "period_start", "period_end"):
            if record[field] is not None:
                parsed_date = date.fromisoformat(record[field])
                if field == "published_at" and parsed_date > boundary:
                    raise ValueError("provided_context_future_date")
        if (
            record["period_start"]
            and record["period_end"]
            and record["period_start"] > record["period_end"]
        ):
            raise ValueError("provided_context_period_invalid")
        result.append(
            dict(origin="provided_context", **{key: record[key] for key in _SOURCE_FIELDS})
        )
    return result


def prepare_provided_context(task, dynamic, *, store, work_item_ids, subject):
    """Called after original owner/task checks, before the actual send boundary."""
    board = task.evidence_board_doc
    if task.raw.get("protocol") != "iqs.company-scan-task/1.0.0" or not board.get("records"):
        return None
    reference = task.raw["evidence_epoch_ref"]
    raw = canonical(board).encode("utf8")
    if reference != dict(
        path="native-derived/evidence-board.json",
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    ):
        raise ValueError("provided_context_original_board_mismatch")
    if (
        dynamic.get("company_ref") != task.company_ref
        or dynamic.get("information_cutoff") != task.raw["information_cutoff"]
    ):
        raise ValueError("provided_context_subject_or_cutoff_mismatch")
    original = {r["evidence_id"]: r for r in board["records"]}
    sent = []
    for record in dynamic.get("evidence", []):
        if record.get("evidence_id") not in original:
            continue  # Later search results do not become provided sources.
        if (
            record != original[record["evidence_id"]]
            or record.get("issuer_ref") != task.company_ref
        ):
            raise ValueError("provided_context_sent_record_mismatch")
        if hashlib.sha256(record["excerpt"].encode("utf8")).hexdigest() != record["excerpt_sha256"]:
            raise ValueError("provided_context_excerpt_mismatch")
        sent.append(record)
    if not sent:
        return None
    members = {}
    for work_id in work_item_ids:
        item = store.get_item(work_id)
        owner = store.get_owner_refresh_binding(work_id)
        if (
            owner is None
            or owner["binding"]["information_cutoff"] != task.raw["information_cutoff"]
            or owner["binding"]["perimeter_sha256"] != subject["perimeter_sha256"]
            or owner["binding"]["subject_key"]
            != subject["analysis_subject_id"] + "@" + str(subject["analysis_subject_revision"])
        ):
            raise ValueError("provided_context_original_owner_required")
        if (
            item["entity_id"] != subject["entity_id"]
            or item["scope"] != subject["scope"]
            or item["scope_id"] != subject["scope_id"]
        ):
            raise ValueError("provided_context_work_subject_mismatch")
        members[work_id] = {
            **{key: item[key] for key in _FIELDS},
            "owner_binding_sha256": owner["binding_sha256"],
        }
    return dict(
        schema="stockqa.provided_context/1.0.0",
        subject_ref=copy.deepcopy(subject),
        company_ref=copy.deepcopy(task.company_ref),
        information_cutoff=task.raw["information_cutoff"],
        evidence_epoch_ref=copy.deepcopy(reference),
        dynamic_context_sha256=digest(dynamic),
        records=_sources(sent, task.raw["information_cutoff"]),
        members=members,
    )


def assert_sent_context(context, prompt):
    """The original physical prompt must contain the exact actual dynamic JSON."""
    # Native search supplies bare user context; the existing research SDK
    # supplies its serialized messages. Inspect only actual user.content text,
    # never arbitrary nested JSON, configuration or a substring of a prompt.
    carriers = [prompt]
    try:
        messages = json.loads(prompt)
    except (ValueError, TypeError):
        messages = None
    if isinstance(messages, list):
        carriers.extend(
            message["content"]
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
        )
    matches = []
    for carrier in carriers:
        for line in carrier.splitlines():
            try:
                node = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(node, dict) and digest(node) == context["dynamic_context_sha256"]:
                matches.append(node)
    if len(matches) != 1:
        raise ValueError("provided_context_not_actually_sent")
    node = matches[0]
    if (
        node.get("company_ref") != context["company_ref"]
        or node.get("information_cutoff") != context["information_cutoff"]
    ):
        raise ValueError("provided_context_sent_subject_mismatch")
    actual = {r["evidence_id"]: r for r in node.get("evidence", [])}
    for summary in context["records"]:
        record = actual.get(summary["evidence_id"])
        if record is None or _sources([record], context["information_cutoff"])[0] != summary:
            raise ValueError("provided_context_not_actually_sent")
        if (
            record.get("issuer_ref") != context["company_ref"]
            or hashlib.sha256(record["excerpt"].encode("utf8")).hexdigest()
            != record["excerpt_sha256"]
        ):
            raise ValueError("provided_context_sent_record_mismatch")


def read_sources(connection, *, work_item_id, attempt_id, receipt):
    """Rebuild only from the original immutable join anchored in its receipt."""
    from .quick_scan_research_intent import _encode, _hash

    anchor = receipt.get("research_intent_binding_sha256")
    if anchor is None:
        return []
    rows = connection.execute(
        "SELECT * FROM quick_scan_research_intent WHERE binding_sha256=?", (anchor,)
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("provided_context_original_intent_missing")
    row = rows[0]
    encoded = row["provided_context_json"] if "provided_context_json" in row.keys() else None
    if encoded is None:
        return []  # Old research intents retain their original exact receipt.
    context = json.loads(encoded)
    members = json.loads(row["work_item_ids_json"])
    binding = {
        k: row[k]
        for k in ("task_sha256", "intent_sha256", "native_attempt_id", "request_payload_sha256")
    }
    binding.update(work_item_ids=members, provided_context_sha256=_hash(context))
    if (
        _encode(context) != encoded
        or _encode(members) != row["work_item_ids_json"]
        or _hash(binding) != anchor
    ):
        raise ValueError("provided_context_original_binding_corrupt")
    if (
        context.get("schema") != "stockqa.provided_context/1.0.0"
        or set(context["members"]) != set(members)
        or work_item_id not in members
    ):
        raise ValueError("provided_context_work_member_mismatch")
    if receipt.get("request_payload_sha256") != row["request_payload_sha256"]:
        raise ValueError("provided_context_original_request_mismatch")
    single = connection.execute(
        "SELECT work_item_id FROM attempt WHERE attempt_id=?", (row["native_attempt_id"],)
    ).fetchone()
    if single is not None:
        if attempt_id != row["native_attempt_id"]:
            raise ValueError("provided_context_original_attempt_mismatch")
        actual_members = [single[0]]
    else:
        packed = connection.execute(
            "SELECT m.work_item_id,m.attempt_id FROM quick_scan_question_pack_member m JOIN quick_scan_question_pack_send s USING(pack_id) WHERE s.budget_attempt_id=?",
            (row["native_attempt_id"],),
        ).fetchall()
        actual_members = sorted(r["work_item_id"] for r in packed)
        if not any(
            r["work_item_id"] == work_item_id
            and attempt_id in {r["attempt_id"], row["native_attempt_id"]}
            for r in packed
        ):
            raise ValueError("provided_context_original_attempt_mismatch")
    if members != actual_members:
        raise ValueError("provided_context_original_members_mismatch")
    item = connection.execute(
        "SELECT * FROM work_item WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    from .quick_scan_owner_refresh_journal import read

    owner = read(connection, work_item_id)
    expected = context["members"][work_item_id]
    if (
        item is None
        or owner is None
        or any(item[k] != expected[k] for k in _FIELDS)
        or owner["binding_sha256"] != expected["owner_binding_sha256"]
    ):
        raise ValueError("provided_context_original_subject_changed")
    subject = context["subject_ref"]
    if (
        owner["binding"]["information_cutoff"] != context["information_cutoff"]
        or owner["binding"]["perimeter_sha256"] != subject["perimeter_sha256"]
        or owner["binding"]["subject_key"]
        != subject["analysis_subject_id"] + "@" + str(subject["analysis_subject_revision"])
    ):
        raise ValueError("provided_context_original_cutoff_or_subject_changed")
    return _sources(context["records"], context["information_cutoff"])
