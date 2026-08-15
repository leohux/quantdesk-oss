# -*- coding: utf-8 -*-
"""QuantDesk HTTP client for validate / backtest / promote."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any


class QuantDeskClient:
    def __init__(self) -> None:
        self.base = os.environ.get("ALPHA_MINER_API", "http://quantdesk:8000").rstrip("/")
        self.user = os.environ.get("ALPHA_MINER_USER", "admin")
        self.password = (
            os.environ.get("ALPHA_MINER_PASSWORD")
            or os.environ.get("ADMIN_PASSWORD")
            or ""
        )
        if not self.password:
            raise RuntimeError(
                "ALPHA_MINER_PASSWORD or ADMIN_PASSWORD env var is required"
            )
        self.token: str | None = None
        self._auth_lock = threading.Lock()

    def _raw_req(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        auth: bool = True,
        token: str | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        tok = token if token is not None else self.token
        if auth and tok:
            headers["X-Access-Token"] = tok
            headers["Authorization"] = f"Bearer {tok}"
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def login(self) -> None:
        with self._auth_lock:
            out = self._raw_req(
                "POST",
                "/api/auth/jwt-login",
                {"password": self.password},
                auth=False,
            )
            self.token = out.get("access_token") or out.get("token")
            if not self.token:
                raise RuntimeError(f"login failed: {out}")

    def _req(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        auth: bool = True,
        _retried: bool = False,
    ) -> Any:
        try:
            return self._raw_req(method, path, data, auth=auth)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            # JWT expired / unauthorized → re-login once and retry
            if auth and exc.code == 401 and not _retried:
                self.login()
                return self._req(method, path, data, auth=auth, _retried=True)
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail[:500]}") from exc

    def validate(self, code: str) -> dict:
        return self._req("POST", "/api/strategies/validate", {"code": code})

    def backtest(
        self,
        *,
        code: str,
        symbols: list[str],
        params: dict,
        start: str = "2021-01-01",
        end: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "code": code,
            "symbols": symbols,
            "params": params,
            "start": start,
        }
        if end:
            payload["end"] = end
        return self._req("POST", "/api/backtest", payload)

    def create_strategy(
        self,
        *,
        name: str,
        type_: str,
        description: str,
        params: dict,
        code: str,
    ) -> dict:
        return self._req(
            "POST",
            "/api/strategies",
            {
                "name": name,
                "type": type_,
                "description": description,
                "params": params,
                "code": code,
            },
        )

    def patch_strategy(self, strategy_id: str, patch: dict) -> dict:
        return self._req("PATCH", f"/api/strategies/{strategy_id}", patch)

    def list_strategies(self) -> list:
        out = self._req("GET", "/api/strategies")
        if isinstance(out, list):
            return out
        return out.get("strategies") or out.get("items") or []
