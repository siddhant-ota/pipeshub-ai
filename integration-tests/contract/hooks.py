"""Schemathesis hooks for the contract tests. Loaded through the `hooks` config key."""

from __future__ import annotations

import sys
from pathlib import Path

import schemathesis

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env import load_env, log_in

load_env()


@schemathesis.auth(refresh_interval=600)
class UserSessionAuth:
    def get(self, case: schemathesis.Case, ctx: schemathesis.AuthContext) -> str:
        return log_in()

    def set(self, case: schemathesis.Case, data: str, ctx: schemathesis.AuthContext) -> None:
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}
