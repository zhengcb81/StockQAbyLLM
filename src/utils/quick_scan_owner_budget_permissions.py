"""Read a current original owner policy before quote, never a new grant.

Only the original deployment configuration selects the fixed native endpoint.
No consumer payload, Python factory, shell command or arbitrary policy file
can be substituted for that native permission-only read.
"""

import copy
import subprocess  # nosec B404 - fixed first-party OS CLI, no shell
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .quick_scan_owner_budget_continuation import accepted_control, canonical, digest
from .quick_scan_owner_refresh import _read
from .quick_scan_refresh_admission import (
    CONFIG_KEYS,
    PATH_KEYS,
    ROOT_KEYS,
    _plain_path,
    _snapshot,
)
from .quick_scan_refresh_consent import (
    MAX_BYTES,
    OwnerConsentEndpoint,
    RefreshConsentClient,
)


def current_policy(config_path, request, *, ledger_path, budget_policy_file, process_env=None):
    config_path = Path(config_path)
    config, config_raw = _snapshot(config_path)
    if (
        set(config) != CONFIG_KEYS
        or config["protocol"] != "stockqa.refresh_admission_config/1.0.0"
        or any(
            not isinstance(config[k], str) or not Path(config[k]).is_absolute() for k in PATH_KEYS
        )
        or config["consumer_id"] != request["consumer_id"]
        or Path(config["work_store"]).resolve() != Path(ledger_path).resolve()
        or Path(config["budget_policy_file"]).resolve() != Path(budget_policy_file).resolve()
    ):
        raise ValueError("owner_exposure_trusted_deployment_mismatch")
    for k in PATH_KEYS - {"work_store", "budget_policy_file"}:
        _plain_path(config[k], directory=True)
    endpoint = OwnerConsentEndpoint({k: config[k] for k in ROOT_KEYS}, process_env=process_env)
    client = RefreshConsentClient(
        endpoint,
        work_store=ledger_path,
        budget_policy_file=budget_policy_file,
        consumer_id=config["consumer_id"],
    )
    source = endpoint.config
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        source["stockwiki_workspace_root"],
        "--iqs-code-root",
        source["iqs_code_root"],
        "--iqs-release-root",
        source["iqs_release_root"],
        "--permissions-request-stdin",
    ]
    try:
        done = subprocess.run(
            command,
            cwd=source["stockwiki_code_root"],
            env=client._environment(),
            input=canonical(request).encode("utf8"),
            capture_output=True,
            timeout=80,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("owner_exposure_native_permissions_unavailable") from error
    if done.returncode != 0 or len(done.stdout) > MAX_BYTES:
        raise ValueError("owner_exposure_native_permissions_rejected")
    result = _read(done.stdout)
    keys = {
        "protocol",
        "authority",
        "owner_policy",
        "owner_state",
        "owner_principal",
        "request_sha256",
        "API_requests",
        "dispatch_started",
        "registered_with_executor",
    }
    if (
        set(result) != keys
        or result["protocol"] != "stockwiki.refresh_permissions_read/1.0.0"
        or result["authority"] != "native_owner_account_and_control_namespace"
        or result["request_sha256"] != digest(request)
        or type(result["API_requests"]) is not int
        or result["API_requests"] != 0
        or result["dispatch_started"] is not False
        or result["registered_with_executor"] is not False
    ):
        raise ValueError("owner_exposure_native_permissions_wire_invalid")
    policy = result["owner_policy"]
    schemas = [
        _read((Path(source["iqs_code_root"]) / "schemas/quick_scan" / name).read_bytes())
        for name in (
            (
                "refresh-v1.5.schema.json"
                if request.get("protocol") == "iqs.refresh_request/1.5.0"
                else "refresh-v1.4.schema.json"
            ),
            "query-v2.schema.json",
            "query-v2.6.schema.json",
        )
    ]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    for field, definition in ((policy, "OwnerPolicy"), (request, "Request")):
        Draft202012Validator(
            {"$ref": schemas[0]["$id"] + "#/$defs/" + definition},
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(field)
    qa = policy["qa"]
    if (
        result["owner_principal"] != policy["owner_principal"]
        or result["owner_state"]["policy_sha256"] != digest(policy)
        or Path(qa["work_store"]).resolve() != Path(ledger_path).resolve()
        or Path(qa["budget_policy_file"]).resolve() != Path(budget_policy_file).resolve()
        or Path(qa["admission_config"]).resolve() != config_path.resolve()
        or Path(qa["code_root"]).resolve() != Path(__file__).resolve().parents[2]
        or _snapshot(config_path)[1] != config_raw
    ):
        raise ValueError("owner_exposure_native_policy_deployment_changed")
    state = result["owner_state"]
    if {i["question_id"] for i in state["items"]} != set(request["question_ids"]) or {
        digest(i["subject_ref"]) for i in state["items"]
    } != {digest(r) for r in request["subject_refs"]}:
        raise ValueError("owner_exposure_native_permission_scope_changed")
    import time

    _, control = accepted_control(policy, request, ledger_path, now=time.time())
    anchors = {digest(a["subject_ref"]): a for a in state["anchors"]}
    for cell in control["cells"]:
        if (
            anchors[digest(cell["subject_ref"])]["manifest_raw_sha256"]
            != cell["manifest_raw_sha256"]
        ):
            raise ValueError("owner_exposure_manifest_version_changed")
    return copy.deepcopy(policy)
