"""Three real temporary stores and public CLIs; explicitly synthetic vendor HTTP.

Only HTTP is substituted, all routing/identity serialization/pre-registration,
checkpoint/seal/import/inspection/reuse run through the existing implementation.
This is software E2E, not a real issuer golden or financial qualification.
"""

import pytest

from tests.integration import test_c06_subject_public_e2e as legacy

actual = legacy.actual


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
@pytest.mark.parametrize("scope", ["entity", "security"])
def test_vendor_full_public_chain(actual, tmp_path, monkeypatch, scope):
    legacy.test_actual_public_authority_owner_registration_client_checkpoint_and_import(
        actual, tmp_path, monkeypatch, scope
    )
