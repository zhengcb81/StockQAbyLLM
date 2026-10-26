"""New delegated settled-control wire is explicit, bounded and hash-exact."""

import pytest

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_decision_authority import StockWikiWorkDecisionAuthority


def seal(value):
    value["record_sha256"] = canonical_sha256(
        {k: v for k, v in value.items() if k != "record_sha256"}
    )
    return value


def fixture(tmp_path):
    client = object.__new__(StockWikiWorkDecisionAuthority)
    client.consumer_id = "synthetic-consumer"
    client.owner = type(
        "Endpoint",
        (),
        {"config": {"stockwiki_workspace_root": str(tmp_path.resolve())}},
    )()
    client.clock = lambda: "2026-10-11T12:01:00Z"
    d = dict(
        kind="known_response_resolution",
        work_item_id="WORK_SYNTHETIC",
        attempt_id="ATTEMPT_SYNTHETIC",
    )
    receipt = dict(
        preview_id="wdp1_" + "a" * 64,
        preview_record_sha256="b" * 64,
        request_sha256="c" * 64,
        outer_request_sha256="d" * 64,
        permission_request_sha256="e" * 64,
        owner_state_sha256="f" * 64,
        policy_sha256="1" * 64,
        owner_store_id="OBS_SYNTHETIC",
        action_id="synthetic-action",
        owner_principal="uid:synthetic",
        confirmed_at="2026-10-11T12:01:00Z",
    )
    binding = {
        **{
            k: receipt[k]
            for k in [
                "preview_id",
                "preview_record_sha256",
                "request_sha256",
                "outer_request_sha256",
                "permission_request_sha256",
                "owner_state_sha256",
                "policy_sha256",
                "owner_store_id",
                "action_id",
            ]
        },
        "kind": d["kind"],
        "purpose": None,
        "work_item_id": d["work_item_id"],
        "attempt_id": d["attempt_id"],
    }
    grant = dict(
        protocol="stockwiki.work_delegation/1.0.0",
        owner_principal="uid:synthetic",
        workspace_root=str(tmp_path.resolve()),
        consumer_id=client.consumer_id,
        agent=dict(
            profile_id="synthetic-agent",
            profile_sha256="a" * 64,
            harness_ref="unit-only",
        ),
        agent_assurance="local_owner_namespace",
        authorization_basis="recorded_user_authorization",
        authorization_reference="synthetic fixture authorization",
        binding=binding,
        created_at="2026-10-11T12:00:00Z",
        expires_at="2026-10-11T13:00:00Z",
    )
    grant["grant_id"] = "wdg1_" + canonical_sha256(
        {
            **{k: grant[k] for k in ["owner_principal", "workspace_root", "consumer_id"]},
            "preview_id": binding["preview_id"],
        }
    )
    seal(grant)
    value = dict(
        confirmation={**receipt, "delegation": grant},
        request=dict(decision_request=d),
        principal="uid:synthetic",
    )
    return client, value


def test_exact_delegated_known_control_and_unusable_retry_are_accepted(tmp_path):
    c, v = fixture(tmp_path)
    c._validate_control_delegation(v)
    v["request"]["decision_request"].update(kind="retry_decision", purpose="retry_unusable_answer")
    v["confirmation"]["delegation"]["binding"].update(
        kind="retry_decision", purpose="retry_unusable_answer"
    )
    seal(v["confirmation"]["delegation"])
    c._validate_control_delegation(v)


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "grant_id",
        "action",
        "attempt",
        "work",
        "scope",
        "policy",
        "request",
        "consumer",
        "workspace",
        "principal",
        "expired",
        "future",
        "too_long",
        "agent",
        "authorization",
        "extra",
        "comparison",
        "retry_unknown",
    ],
)
def test_modified_grant_even_resealed_does_not_authenticate_other_control(tmp_path, fault):
    c, v = fixture(tmp_path)
    g = v["confirmation"]["delegation"]
    b = g["binding"]
    if fault == "hash":
        g["record_sha256"] = "0" * 64
    elif fault == "grant_id":
        g["grant_id"] = "wdg1_" + "0" * 64
    elif fault == "action":
        b["action_id"] = "another-action"
    elif fault == "attempt":
        b["attempt_id"] = "ATTEMPT_OTHER"
    elif fault == "work":
        b["work_item_id"] = "WORK_OTHER"
    elif fault == "scope":
        b["permission_request_sha256"] = "0" * 64
    elif fault == "policy":
        b["policy_sha256"] = "0" * 64
    elif fault == "request":
        b["request_sha256"] = "0" * 64
    elif fault == "consumer":
        g["consumer_id"] = "other"
    elif fault == "workspace":
        g["workspace_root"] = str(tmp_path / "copy")
    elif fault == "principal":
        g["owner_principal"] = "uid:other"
    elif fault == "expired":
        c.clock = lambda: "2026-10-11T14:00:00Z"
    elif fault == "future":
        g["created_at"] = "2026-10-11T12:02:00Z"
    elif fault == "too_long":
        g["expires_at"] = "2026-10-13T12:00:00Z"
    elif fault == "agent":
        g["agent"]["profile_sha256"] = "unknown"
    elif fault == "authorization":
        g["authorization_basis"] = "consumer_says_approved"
    elif fault == "extra":
        g["approved"] = True
    else:
        v["request"]["decision_request"].update(
            kind="retry_decision",
            purpose="comparison" if fault == "comparison" else "retry_unknown_answer",
        )
        b.update(kind="retry_decision", purpose=v["request"]["decision_request"]["purpose"])
    if fault != "hash":
        seal(g)
    with pytest.raises((ValueError, KeyError)):
        c._validate_control_delegation(v)
