"""Environment for the contract tests: env files, base URL and the user session."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

CONTRACT_DIR = Path(__file__).resolve().parent
INTEGRATION_TESTS_DIR = CONTRACT_DIR.parent

# `helper.*` lives in integration-tests/ and is imported as a namespace package.
if str(INTEGRATION_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_TESTS_DIR))

# Set by `plan`, which has no PipesHub to log in to.
STATIC_TOKEN_ENV = "CONTRACT_STATIC_TOKEN"


def load_env() -> None:
    """Load .env, then .env.local or .env.prod, the same way integration-tests/conftest.py does."""
    load_dotenv(INTEGRATION_TESTS_DIR / ".env", override=True)
    test_env = os.getenv("PIPESHUB_TEST_ENV", "").strip().lower()
    if test_env in ("local", "prod"):
        load_dotenv(INTEGRATION_TESTS_DIR / f".env.{test_env}", override=True)


def base_url() -> str:
    url = os.getenv("PIPESHUB_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("PIPESHUB_BASE_URL is not set in integration-tests/.env or .env.local")
    return url


def api_url() -> str:
    return f"{base_url()}/api/v1"


def log_in() -> str:
    """Return a session JWT for the test user.

    The contract tests act as a user: conversations, searches and agents belong
    to one. An OAuth client_credentials token has no user of its own.
    """
    static = os.getenv(STATIC_TOKEN_ENV)
    if static:
        return static
    from helper.local_auth import obtain_user_session_token

    return obtain_user_session_token(base_url())
