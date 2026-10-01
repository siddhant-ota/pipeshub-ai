"""Shared fixtures for the response-validation suite."""

from __future__ import annotations

import os
from typing import Optional

import pytest

from helper.clients.config_client import ConfigClient


def _smtp_env() -> Optional[dict]:
    """Build an SMTP config from env, or None if not configured.

    CI provides SMTP_HOST/PORT/USERNAME/PASSWORD as secrets. Locally, point
    these at Mailpit (SMTP_HOST=mailpit SMTP_PORT=1025, no credentials).
    """
    host = os.getenv("SMTP_HOST")
    port = os.getenv("SMTP_PORT")
    if not host or not port:
        return None
    username = os.getenv("SMTP_USERNAME", "")
    return {
        "host": host,
        "port": int(port),
        "username": username,
        "password": os.getenv("SMTP_PASSWORD", ""),
        "fromEmail": os.getenv("SMTP_FROM_EMAIL") or username or "no-reply@example.com",
    }


@pytest.fixture(scope="session")
def smtp_configured(config_client: ConfigClient) -> None:
    """Configure backend SMTP from the SMTP_* env for SMTP-gated routes.

    Any response-validation test that hits a route behind smtpConfigCheck
    (bulk invite, OTP login, forgot-password) can depend on this. Skips the
    dependent test(s) when SMTP_HOST/SMTP_PORT are unset, so local runs without
    a mail server stay green.
    """
    smtp = _smtp_env()
    if smtp is None:
        pytest.skip("SMTP_HOST/SMTP_PORT not set — skipping SMTP-dependent test")
    resp = config_client.create_smtp_config(**smtp)
    assert resp.status_code in (200, 201), (
        f"Failed to configure SMTP: {resp.status_code} {resp.text}"
    )


@pytest.fixture(scope="session")
def smtp_ready(request: pytest.FixtureRequest, config_client: ConfigClient) -> str:
    """SMTP settings exist on the deployment, for routes behind smtpConfigCheck.

    Unlike ``smtp_configured`` this leaves settings that the deployment already
    has as they are. Only a deployment with none gets those of the SMTP_* env,
    and the API cannot remove them again. Returns which of the two it was.
    """
    resp = config_client.get("/smtpConfig/status")
    assert resp.status_code == 200, f"SMTP status: {resp.status_code} {resp.text}"
    if resp.json().get("configured"):
        return "the deployment already has SMTP settings"
    request.getfixturevalue("smtp_configured")
    return "written from the SMTP_* environment; the API cannot remove them"
