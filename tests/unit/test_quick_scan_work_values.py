"""Paid-work value codecs must work without starting the native ledger."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE = "src.utils.quick_scan_work_store"
SAMPLE = {
    "provider": "fixture",
    "source_urls": ["https://issuer.example.invalid/report?tag=operating&api_key=ignore#comments"],
    "opaque": "drop",
    "completed_at": "2026-11-25T00:00:00Z",
    "usage": {
        "schema": "quick-scan-usage-v1",
        "input_tokens": 7,
        "cached_input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "output_tokens": 3,
        "reasoning_output_tokens": 1,
        "search_tool_calls": 0,
    },
}
FROZEN_RECEIPT_SHA = "8093c8643d57443777705dfd8b79be31506210c200f358de168d9e1a8e55752b"


@pytest.mark.parametrize(
    "exercise",
    [
        """from src.utils import quick_scan_observation_context as context
assert context._evidence_url_identity(
    'HTTPS://issuer.example.invalid/report?tag=operating#page',
) == 'https://issuer.example.invalid/report?tag=operating'
try:
    context._evidence_url_identity('https://issuer.example.invalid/report?token=discard')
except ValueError as error:
    assert str(error) == 'invalid or sensitive evidence source URL'
else:
    raise AssertionError('credential-bearing evidence was accepted')
""",
        """from src.utils import quick_scan_provided_context as context
context._sources([{
    'evidence_id': 'proof', 'source_url': 'https://issuer.example.invalid/report?token=discard',
    'published_at': '2026-11-01', 'period_start': None, 'period_end': None,
    'excerpt_sha256': 'a' * 64,
}], '2026-11-25')""",
        """from src.interfaces.quick_scan_work_values import cost_to_micros
assert cost_to_micros(0.0025, 'settled cost') == 2500
""",
    ],
    ids=("evidence-url", "provided-source", "value-leaf"),
)
def test_cold_value_only_consumer_does_not_load_work_store(tmp_path, exercise):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"}
    }
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    program = exercise + f"\nimport sys\nassert {STORE!r} not in sys.modules, 'native store loaded'"
    result = subprocess.run(
        [sys.executable, "-B", "-c", program],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_receipt_digest_matches_released_value_and_old_store_aliases():
    from src.interfaces import quick_scan_work_values as values
    from src.utils import quick_scan_work_store as legacy

    assert values.quick_scan_receipt_sha256(SAMPLE) == FROZEN_RECEIPT_SHA
    assert legacy.quick_scan_receipt_sha256(SAMPLE) == FROZEN_RECEIPT_SHA
    assert values.sanitize_transport_receipt(SAMPLE) == legacy.sanitize_transport_receipt(SAMPLE)
    assert values.sanitize_transport_receipt(SAMPLE)["source_urls"] == [
        "https://issuer.example.invalid/report?tag=operating"
    ]
    assert legacy.api_work_store_budget_policy_projection is values.budget_policy_projection
    assert legacy.api_work_store_sha256 is values.validate_sha256
    assert legacy.api_work_store_canonical_source_urls is values.canonical_source_urls
