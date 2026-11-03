"""Fail fast, with an actionable message, when local GCP credentials are stale.

THE PROBLEM THIS SOLVES
    Local development uses ``impersonated_service_account`` config files under
    ~/gcp-creds/<project>/. Their ``source_credentials`` block is an EMBEDDED
    SNAPSHOT of the Application Default Credentials, not a reference to them. So
    ``gcloud auth application-default login`` issues a new refresh token and
    invalidates the old one — which leaves every snapshot holding a dead token.

    Google's error for this says:

        RefreshError: Reauthentication is needed.
            Please run `gcloud auth application-default login` to reauthenticate.

    That instruction is WRONG in this situation, and expensively so: the ADC is
    fine, you probably just ran that command, and the stale thing is the copy.
    Worse, the Pub/Sub subscriber treats the resulting 503 as recoverable and
    reopens the stream forever, so the real cause scrolls past in a loop of
    identical tracebacks rather than stopping anything.

    This module turns that into one line, printed once, naming the stale file and
    the script that fixes it.

THIS IS THE SINGLE HOME
    All three services ran this at startup and each held a BYTE-IDENTICAL copy,
    because they shared no library. Named `gcp_preflight` here rather than
    `auth_preflight`: it is about GOOGLE CLOUD credentials, not about
    authenticating a user — `apex_common.auth` is that, and conflating the two
    is how it came to sit beside the JWT code in the first place.

    `_FIX_SCRIPT` names a path in task-service-agent deliberately. The script
    exists only there, and one run of it repairs the shared ADC snapshot every
    service reads, so all three correctly point at the same one.

CLOUD RUN IS UNAFFECTED
    There is no credentials FILE there — the runtime service account supplies the
    identity through the metadata server, and nothing can go stale. The detailed
    diagnosis below only engages when GOOGLE_APPLICATION_CREDENTIALS points at an
    impersonated_service_account file, so this is inert in production.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_FIX_SCRIPT = "task-service-agent/scripts/refresh_impersonation_creds.sh"


class StaleCredentialsError(RuntimeError):
    """Local impersonation credentials hold a refresh token that is no longer valid."""


def _impersonation_file() -> Path | None:
    """The GOOGLE_APPLICATION_CREDENTIALS file, if it is an impersonation config."""
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        if json.loads(path.read_text()).get("type") == "impersonated_service_account":
            return path
    except (OSError, ValueError):
        return None
    return None


_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _mtime(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _age(path: Path) -> str:
    mtime = _mtime(path)
    if mtime is None:
        return "unknown"
    hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
    return f"{mtime:%Y-%m-%d %H:%M} UTC ({hours:.0f}h ago)"


def _base_adc_works() -> bool:
    """Can the UNDERLYING Application Default Credentials still mint a token?

    This is the question that decides the advice, and the first version of this
    module never asked it — it asserted "re-running login will NOT help", which
    is right when the snapshot is stale but WRONG when the ADC itself has
    expired. Getting that backwards sends someone re-snapshotting a dead token,
    which reports success and fixes nothing.
    """
    import google.auth
    import google.auth.transport.requests as grequests

    saved = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(grequests.Request())
        return True
    except Exception:
        return False
    finally:
        if saved is not None:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = saved


def verify_credentials(*, fail_fast: bool = True) -> None:
    """Check that Google credentials can actually mint a token.

    Existing is not the same as working: a stale impersonation config loads and
    parses perfectly and only fails at the first API call, which for the Pub/Sub
    subscriber means an endless reopen loop rather than a startup failure.

    Args:
        fail_fast: raise StaleCredentialsError on failure. False logs and returns,
            for callers that would rather degrade than refuse to start.
    """
    try:
        import google.auth
        import google.auth.transport.requests as grequests
    except ImportError:  # pragma: no cover - google libs always present here
        return

    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(grequests.Request())
    except Exception as exc:
        config = _impersonation_file()
        if config is not None and "Reauthentication is needed" in str(exc):
            # WHICH of the two is stale decides the fix, so test rather than assume.
            base_ok = _base_adc_works()
            snap, adc = _mtime(config), _mtime(_ADC_PATH)
            order = ""
            if snap and adc:
                order = (
                    f"  Your ADC was refreshed {_age(_ADC_PATH)}; this snapshot is "
                    f"from {_age(config)} — "
                    + ("the snapshot PREDATES it, which is the whole problem.\n"
                       if snap < adc else
                       "the snapshot is NEWER, so something else invalidated it.\n")
                )
            if base_ok:
                message = (
                    f"GCP credentials are STALE, not missing.\n"
                    f"  {config}\n"
                    f"{order}"
                    f"  The snapshot embeds a COPY of your Application Default\n"
                    f"  Credentials, and re-authenticating invalidates that copy.\n"
                    f"  YOUR ADC IS FINE — verified just now — so logging in again\n"
                    f"  will not help. Re-snapshot it instead:\n"
                    f"      {_FIX_SCRIPT}\n"
                    f"  (Cloud Run is unaffected: it uses its runtime service "
                    f"account, not a file.)"
                )
            else:
                message = (
                    f"GCP credentials are stale AND the base ADC is expired too.\n"
                    f"  {config}\n"
                    f"{order}"
                    f"  BOTH steps are needed, IN THIS ORDER — re-snapshotting\n"
                    f"  first would copy a dead token and report success:\n"
                    f"      gcloud auth application-default login\n"
                    f"      {_FIX_SCRIPT}"
                )
        else:
            message = (
                f"GCP credentials could not be refreshed: {exc}\n"
                f"  Run `gcloud auth application-default login`, then "
                f"{_FIX_SCRIPT} if you use local impersonation configs."
            )

        if fail_fast:
            raise StaleCredentialsError(message) from exc
        logger.error("%s", message)
        return

    config = _impersonation_file()
    if config is not None:
        logger.info("GCP credentials OK (impersonation config %s)", config.name)
    else:
        logger.info("GCP credentials OK")
