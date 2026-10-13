"""Actual native CLI/main from old Flash receipt to approved Pro; fake HTTP only."""

import pytest

from tests.integration.test_c06_subject_public_e2e import actual
from tests.integration.test_controlled_pack_main_public import run_public_main_pack3


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_explicit_new_model_public_admission_main_and_keyless_recovery(
    actual, tmp_path, monkeypatch, capsys
):
    run_public_main_pack3(actual, tmp_path, monkeypatch, capsys, target_model="mimo-v2.6-pro")
