"""Native consent -> exact frozen producer inputs -> original Q13 action.

Consumer input is ONE confirmation ID. No consumer work specifications,
commands, confirmation flags, model calls, leases or budget reservations.
This service only registers work; the original runner still owns execution.
"""

import copy
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import quick_scan_refresh_admission_journal as journal
from .quick_scan_owner_refresh import (
    OwnerRefreshClient,
    OwnerRefreshRejected,
    routing_fingerprints,
)
from .quick_scan_refresh_batch import register_refresh_action
from .quick_scan_refresh_consent import OwnerConsentEndpoint, RefreshConsentClient
from .quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    strict_json_loads,
)
from .quick_scan_work_store import SCHEMA_VERSION, QuickScanWorkStore

MAX_BYTES = 8 * 1024 * 1024
ROOT_KEYS = {
    "stockwiki_code_root",
    "stockwiki_workspace_root",
    "iqs_code_root",
    "iqs_release_root",
}
PATH_KEYS = ROOT_KEYS | {
    "execution_inputs_root",
    "work_store",
    "budget_policy_file",
    "runs_dir",
}
CONFIG_KEYS = PATH_KEYS | {"protocol", "consumer_id"}
BUNDLE_FILES = (
    "manifest.json",
    "identity.json",
    "authority.json",
    "owner-config.json",
    "questions.json",
)


class AdmissionRejected(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _plain_path(path, *, directory=False):
    """Check before resolve/open, including Windows junction ancestors."""
    path = Path(path).absolute()
    try:
        for current in (path, *path.parents):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AdmissionRejected("refresh_admission_link_forbidden")
        if directory:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise AdmissionRejected("refresh_admission_directory_required")
        else:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AdmissionRejected("refresh_admission_regular_input_required")
    except OSError as error:
        raise AdmissionRejected("refresh_admission_input_unavailable") from error
    return path


def _snapshot(path):
    path = _plain_path(path)
    try:
        before = path.lstat()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(MAX_BYTES + 1)
        after = path.lstat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
        if (
            len(raw) > MAX_BYTES
            or len(raw) != before.st_size
            or any(
                getattr(before, k) != getattr(opened, k) or getattr(before, k) != getattr(after, k)
                for k in keys
            )
            or getattr(after, "st_file_attributes", 0) & 0x400
        ):
            raise AdmissionRejected("refresh_admission_input_changed_or_too_large")
        document = strict_json_loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            raise AdmissionRejected("refresh_admission_object_required")
        return document, raw
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, AdmissionRejected):
            raise
        raise AdmissionRejected("refresh_admission_strict_input_required") from error


def safe_read(path):
    return _snapshot(path)[0]


def match_question(item, bundle, *, information_cutoff):
    """Exact SubjectRef and question metadata, after original producer validation."""
    try:
        authority = bundle["authority"]
        context = authority["observation_context"]
        if (
            authority["schema_version"] != "3.0.0"
            or context["schema"] != "stockqa.quick_scan_observation_context/2.0.0"
        ):
            raise ValueError("contract")
        qid = item["question_id"]
        rows = [row for row in bundle["manifest"]["questions"] if row["id"] == qid]
        if len(rows) != 1:
            raise ValueError("question")
        row = context["questions"][qid]
        binding = row["subject_binding"]
        if (
            canonical_bytes(binding["subject_ref"]) != canonical_bytes(item["subject_ref"])
            or binding["identity_revision"] != bundle["identity"]["identity_revision"]
            or item["subject_ref"]["entity_id"] != bundle["identity"]["entity_id"]
            or row["metadata"]["field_id"] != item["field_id"]
            or (rows[0].get("field_id") or rows[0].get("metric_id")) != item["field_id"]
            or row["metadata"]["information_cutoff"] != information_cutoff
        ):
            raise ValueError("scope")
        return copy.deepcopy(row)
    except (KeyError, TypeError, ValueError) as error:
        raise AdmissionRejected("refresh_admission_subject_question_mismatch") from error


class RefreshAdmissionService:
    def __init__(self, config_path, *, process_env=None):
        self.config_path = Path(config_path).absolute()
        self.config, self.config_raw = _snapshot(self.config_path)
        c = self.config
        if (
            set(c) != CONFIG_KEYS
            or c["protocol"] != "stockqa.refresh_admission_config/1.0.0"
            or not isinstance(c["consumer_id"], str)
            or not re.fullmatch("[A-Za-z0-9_.-]{1,160}", c["consumer_id"])
            or any(not isinstance(c[k], str) or not Path(c[k]).is_absolute() for k in PATH_KEYS)
        ):
            raise AdmissionRejected("refresh_admission_deployment_invalid")
        for key in PATH_KEYS - {"work_store", "budget_policy_file"}:
            _plain_path(c[key], directory=True)
        _plain_path(c["work_store"])
        self.budget, self.budget_raw = _snapshot(c["budget_policy_file"])
        self.environment = process_env
        self.endpoint = OwnerConsentEndpoint({k: c[k] for k in ROOT_KEYS}, process_env=process_env)
        self.bridge = RefreshConsentClient(
            self.endpoint,
            work_store=c["work_store"],
            budget_policy_file=c["budget_policy_file"],
            consumer_id=c["consumer_id"],
        )
        # Use original methods without invoking the creating/migrating initializer.
        self.store = QuickScanWorkStore.__new__(QuickScanWorkStore)
        self.store.path, self.store.clock = Path(c["work_store"]), time.time
        self.store._transaction_state = threading.local()
        self._ledger_ready()

    def _ledger_ready(self):
        path = _plain_path(self.store.path)
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise AdmissionRejected("refresh_admission_ledger_busy")
        try:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise AdmissionRejected("refresh_admission_explicit_setup_required")
                self.store._validate_schema(connection, schema_version=SCHEMA_VERSION)
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, AdmissionRejected):
                raise
            raise AdmissionRejected("refresh_admission_ledger_invalid") from error

    def _record(self, confirmation_id):
        with closing(self.store._connect()) as connection:
            return journal.read_record(connection, confirmation_id)

    def _bundle(self, ref, policy):
        from src.config.json_config_manager import JSONConfigManager
        from src.runners.llm_runner import load_identity_snapshot

        from .quick_scan_c06_authority import load_c06_authority
        from .quick_scan_question_manifest import (
            bind_manifest_to_questions,
            load_question_manifest,
        )

        directory = Path(self.config["execution_inputs_root"]) / (
            "subject_" + canonical_sha256(ref)
        )
        _plain_path(directory, directory=True)
        snapshots = {name: _snapshot(directory / name) for name in BUNDLE_FILES}
        # Original loaders read only byte copies of the already strict snapshot.
        with tempfile.TemporaryDirectory(
            prefix="admission-", dir=self.config["runs_dir"]
        ) as temporary:
            frozen = Path(temporary)
            for name, (_, raw) in snapshots.items():
                (frozen / name).write_bytes(raw)
            identity = load_identity_snapshot(
                str(frozen / "identity.json"), expected_entity_id=ref["entity_id"]
            )
            manifest = load_question_manifest(frozen / "manifest.json")
            questions = JSONConfigManager(str(frozen / "questions.json")).load_question_items()
            bind_manifest_to_questions(manifest, questions)
            authority = load_c06_authority(
                frozen / "authority.json",
                manifest=manifest,
                identity_snapshot_sha256=identity["identity_snapshot_sha256"],
            )
            owner = OwnerRefreshClient(frozen / "owner-config.json", process_env=self.environment)
        expected = {k: self.config[k] for k in ROOT_KEYS}
        if (
            any(Path(owner.config[k]).resolve() != Path(v).resolve() for k, v in expected.items())
            or owner.config["subject_key"]
            != ref["analysis_subject_id"] + "@" + str(ref["analysis_subject_revision"])
            or owner.config["scope"] != ("segment" if ref["scope"] == "segment" else "entity")
            or owner.config["scope_id"]
            != (ref["scope_id"] if ref["scope"] == "segment" else ref["entity_id"])
            or owner.config["ttl_hours"] != policy["ttl_hours"]
        ):
            raise AdmissionRejected("refresh_admission_owner_deployment_mismatch")
        return dict(
            identity=identity,
            manifest=manifest,
            questions={q.question_id: q for q in questions},
            authority=authority,
            owner=owner,
            snapshots=snapshots,
            directory=directory,
        )

    def _inputs_unchanged(self, bundles):
        if (
            _snapshot(self.config_path)[1] != self.config_raw
            or _snapshot(self.config["budget_policy_file"])[1] != self.budget_raw
        ):
            raise AdmissionRejected("refresh_admission_deployment_changed")
        for bundle in bundles.values():
            for name, (_, raw) in bundle["snapshots"].items():
                if _snapshot(bundle["directory"] / name)[1] != raw:
                    raise AdmissionRejected("refresh_admission_execution_input_changed")

    def _proof_inputs(self, proof):
        bundles = {}
        for item in proof["preview"]["items"]:
            key = canonical_sha256(item["subject_ref"])
            if key not in bundles:
                bundles[key] = self._bundle(item["subject_ref"], proof["owner_policy"])
            match_question(
                item,
                bundles[key],
                information_cutoff=proof["request"]["source_query"]["payload"][
                    "information_cutoff"
                ],
            )
        return bundles

    def _action_binding(self, proof, bundles):
        preview = proof["preview"]
        input_sha = canonical_sha256(
            dict(
                config_raw_sha256=hashlib.sha256(self.config_raw).hexdigest(),
                budget_raw_sha256=hashlib.sha256(self.budget_raw).hexdigest(),
                bundles={
                    key: {
                        name: hashlib.sha256(raw).hexdigest()
                        for name, (_, raw) in bundle["snapshots"].items()
                    }
                    for key, bundle in bundles.items()
                },
            )
        )
        return dict(
            confirmation_id=proof["confirmation"]["confirmation_id"],
            confirmation_sha256=canonical_sha256(proof["confirmation"]),
            preview_id=preview["preview_id"],
            scope_sha256=preview["scope_sha256"],
            policy_sha256=preview["policy_sha256"],
            execution_inputs_sha256=input_sha,
        )

    def read_registered(self, confirmation_id):
        """Authenticate an existing action; never register from a replay read."""
        if not isinstance(confirmation_id, str) or not re.fullmatch(
            "rc[12]_[a-f0-9]{64}", confirmation_id
        ):
            raise AdmissionRejected("refresh_admission_confirmation_id_invalid")
        self._ledger_ready()
        record = self._record(confirmation_id)
        if record is None:
            raise AdmissionRejected("refresh_execution_registered_action_required")
        proof = self.bridge.inspect(confirmation_id, mode="current_permissions")
        bundles = self._proof_inputs(proof)
        if canonical_bytes(record["inputs"]["action"]) != canonical_bytes(
            self._action_binding(proof, bundles)
        ):
            raise AdmissionRejected("refresh_admission_idempotency_conflict")
        return proof, bundles, record

    def admit(self, confirmation_id):
        if not isinstance(confirmation_id, str) or not re.fullmatch(
            "rc[12]_[a-f0-9]{64}", confirmation_id
        ):
            raise AdmissionRejected("refresh_admission_confirmation_id_invalid")
        try:
            self._ledger_ready()
            existing = self._record(confirmation_id)
            proof = self.bridge.inspect(
                confirmation_id,
                mode="current_permissions" if existing else "fresh_registration",
            )
            preview, policy = proof["preview"], proof["owner_policy"]
            bundles = self._proof_inputs(proof)
            action = self._action_binding(proof, bundles)
            sessions = []
            if existing:
                value = existing["inputs"]
                if canonical_bytes(value["action"]) != canonical_bytes(action):
                    raise AdmissionRejected("refresh_admission_idempotency_conflict")
                specifications, references = value["specifications"], value.get("references")
                run_id, scan_id = value["run_id"], value["scan_id"]
                # Do not replan the old action against its own new pending work.
                for bundle in bundles.values():
                    bundle["owner"].current(
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    )
            else:
                specifications, references = [], []
                for bundle in bundles.values():
                    bundle["session"] = bundle["owner"].prepare(
                        bundle["manifest"],
                        self.store,
                        identity=bundle["identity"],
                        provider=preview["provider"],
                        model=preview["model"],
                        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        observation_context=bundle["authority"]["observation_context"],
                    )
                    sessions.append(bundle["session"])
                for item in preview["items"]:
                    bundle = bundles[canonical_sha256(item["subject_ref"])]
                    session, qid = bundle["session"], item["question_id"]
                    planned = session.fields[qid]
                    controlled_binding = None
                    if proof["request"]["protocol"] in {
                        "iqs.refresh_request/1.2.0",
                        "iqs.refresh_request/1.3.0",
                    }:
                        from .quick_scan_owner_refresh_journal import native_control

                        control = item["owner_control"]
                        if control["cohort_id"] != proof["request"]["cohort_id"]:
                            raise AdmissionRejected("refresh_admission_control_cohort_mismatch")
                        controlled_binding = session.binding(qid, owner_control=control)
                        synthetic_target = dict(
                            entity_id=item["subject_ref"]["entity_id"],
                            question_id=qid,
                            scope=item["subject_ref"]["scope"],
                            scope_id=item["subject_ref"]["scope_id"],
                            identity_revision=bundle["identity"]["identity_revision"],
                            generation=control["target_generation"],
                        )
                        with closing(self.store._connect()) as connection:
                            source = native_control(
                                connection,
                                synthetic_target,
                                controlled_binding,
                                now=self.store._now(),
                                require_ready=True,
                            )
                        planned = dict(
                            planned,
                            decision="dispatch_new_generation",
                            generation=control["target_generation"],
                            reuse_observation_id=None,
                            existing_work_status=source["status"],
                        )
                    for key in (
                        "decision",
                        "generation",
                        "reuse_observation_id",
                        "existing_work_status",
                    ):
                        if planned.get(key) != item.get(key):
                            raise AdmissionRejected("refresh_admission_plan_changed")
                    if item["decision"] == "reuse":
                        result = session.reference(bundle["questions"][qid])
                        if result is None:
                            raise AdmissionRejected("refresh_admission_reference_unavailable")
                        original = result.metadata["owner_observation_reference"]
                        references.append(
                            dict(
                                subject_ref=copy.deepcopy(item["subject_ref"]),
                                question_id=qid,
                                field_id=item["field_id"],
                                observation_id=original["observation_id"],
                                payload_sha256=original["payload_sha256"],
                            )
                        )
                        continue
                    if item["decision"] in {
                        "dispatch_new_work",
                        "dispatch_new_generation",
                    }:
                        binding = (
                            controlled_binding
                            if controlled_binding is not None
                            else session.binding(qid)
                        )
                    elif item["decision"] == "resume_existing_work":
                        work = session.existing_work(qid)
                        if work is None:
                            raise AdmissionRejected("refresh_admission_existing_work_missing")
                        record = self.store.get_owner_refresh_binding(work["work_item_id"])
                        if record is None:
                            raise AdmissionRejected("refresh_admission_existing_binding_missing")
                        binding = record["binding"]
                    else:
                        raise AdmissionRejected("refresh_admission_deferred_scope")
                    scope = session.scope_bindings[qid]
                    specifications.append(
                        dict(
                            bundle["identity"],
                            question_id=qid,
                            generation=item["generation"],
                            scope=scope["scope"],
                            scope_id=scope["scope_id"],
                            question_fingerprint=hashlib.sha256(
                                bundle["questions"][qid].text.strip().encode("utf-8")
                            ).hexdigest(),
                            routing_fingerprint=canonical_sha256(
                                dict(
                                    subject_key=binding["subject_key"],
                                    request_identity_key=binding["request_identity_key"],
                                )
                            ),
                            owner_refresh_binding=binding,
                        )
                    )
                token = confirmation_id[4:]
                run_id, scan_id = "refresh-run-" + token, "refresh-scan-" + token

            def recheck(mode):
                self._inputs_unchanged(bundles)
                current = self.bridge.inspect(confirmation_id, mode=mode)
                if any(
                    canonical_bytes(current[key]) != canonical_bytes(proof[key])
                    for key in ("confirmation", "preview", "owner_policy")
                ):
                    raise AdmissionRejected("refresh_admission_native_scope_changed")
                for session in sessions:
                    session.check()

            receipt = register_refresh_action(
                self.store,
                specifications,
                action=action,
                run_id=run_id,
                scan_id=scan_id,
                recheck=recheck,
                references=references,
                continuation=(
                    {"proof": proof, "bundles": bundles}
                    if proof["request"]["protocol"]
                    in {"iqs.refresh_request/1.4.0", "iqs.refresh_request/1.5.0"}
                    else None
                ),
            )
            committed = self._record(confirmation_id)
            if committed is None or canonical_bytes(committed["receipt"]) != canonical_bytes(
                receipt
            ):
                raise AdmissionRejected("refresh_admission_commit_unavailable")
            recheck("current_permissions")
            return dict(
                protocol="stockqa.refresh_admission/1.0.0",
                status="already_registered" if existing else "accepted",
                handoff_status="accepted",
                permission_authenticated=True,
                registered_with_executor=True,
                dispatch_started=False,
                API_requests=0,
                registration=copy.deepcopy(receipt),
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
            if isinstance(error, AdmissionRejected):
                raise
            raise AdmissionRejected("refresh_admission_native_or_producer_rejected") from error
