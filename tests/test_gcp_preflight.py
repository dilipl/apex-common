"""Tests for the stale-credential preflight check.

The failure this guards against cost several debugging rounds: Google's own
error tells you to run the exact command you just ran, and the Pub/Sub
subscriber hides it in an endless reopen loop.
"""

import json
from unittest.mock import patch

import pytest

from apex_common.gcp_preflight import StaleCredentialsError, verify_credentials


def _impersonation_config(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({
        "type": "impersonated_service_account",
        "service_account_impersonation_url": "https://example/sa:generateAccessToken",
        "source_credentials": {"type": "authorized_user"},
    }))
    return p


class TestStaleImpersonation:
    def test_stale_snapshot_with_a_healthy_adc_says_re_snapshot(
            self, tmp_path, monkeypatch):
        """ADC fine, snapshot stale -> re-snapshot, do NOT log in again."""
        cfg = _impersonation_config(tmp_path)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cfg))

        with patch("google.auth.default", side_effect=Exception(
                "Reauthentication is needed. Please run `gcloud auth "
                "application-default login` to reauthenticate.")), \
             patch("apex_common.gcp_preflight._base_adc_works", return_value=True):
            with pytest.raises(StaleCredentialsError) as exc:
                verify_credentials()

        msg = str(exc.value)
        assert "STALE, not missing" in msg
        assert str(cfg) in msg
        assert "will not help" in msg.lower()
        assert "refresh_impersonation_creds.sh" in msg

    def test_dead_adc_says_log_in_FIRST_then_re_snapshot(self, tmp_path, monkeypatch):
        """The case the first version got WRONG.

        When the base ADC is also expired, "re-snapshot" alone copies a dead
        token into every config and reports success — so the order matters and
        the message must say both steps.
        """
        cfg = _impersonation_config(tmp_path)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cfg))

        with patch("google.auth.default", side_effect=Exception(
                "Reauthentication is needed.")), \
             patch("apex_common.gcp_preflight._base_adc_works", return_value=False):
            with pytest.raises(StaleCredentialsError) as exc:
                verify_credentials()

        msg = str(exc.value)
        assert "base ADC is expired too" in msg
        assert "gcloud auth application-default login" in msg
        assert "refresh_impersonation_creds.sh" in msg
        # It must NOT tell the user that logging in will not help.
        assert "will not help" not in msg.lower()

    def test_non_impersonation_failure_gets_the_ordinary_advice(self, monkeypatch):
        """Without an impersonation config, re-login genuinely IS the fix."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        with patch("google.auth.default", side_effect=Exception(
                "Reauthentication is needed.")):
            with pytest.raises(StaleCredentialsError) as exc:
                verify_credentials()
        msg = str(exc.value)
        assert "STALE, not missing" not in msg
        assert "gcloud auth application-default login" in msg

    def test_fail_fast_false_logs_instead_of_raising(self, tmp_path, monkeypatch, caplog):
        cfg = _impersonation_config(tmp_path)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cfg))
        with patch("google.auth.default", side_effect=Exception(
                "Reauthentication is needed.")), \
             patch("apex_common.gcp_preflight._base_adc_works", return_value=True):
            verify_credentials(fail_fast=False)
        assert "STALE, not missing" in caplog.text

    def test_working_credentials_pass_quietly(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        with patch("google.auth.default", return_value=(_Creds(), "proj")):
            verify_credentials()          # must not raise

    def test_a_service_account_key_is_not_treated_as_impersonation(
            self, tmp_path, monkeypatch):
        """Only impersonated_service_account files have the embedded-copy problem."""
        p = tmp_path / "key.json"
        p.write_text(json.dumps({"type": "service_account", "client_email": "x@y"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(p))
        with patch("google.auth.default", side_effect=Exception(
                "Reauthentication is needed.")):
            with pytest.raises(StaleCredentialsError) as exc:
                verify_credentials()
        assert "STALE, not missing" not in str(exc.value)

    def test_missing_credentials_file_does_not_crash_the_check(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "nope.json"))
        with patch("google.auth.default", side_effect=Exception("boom")):
            with pytest.raises(StaleCredentialsError):
                verify_credentials()


class _Creds:
    def refresh(self, request):
        return None
