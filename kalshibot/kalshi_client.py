"""Minimal Kalshi trading API client.

Auth: Kalshi signs every private request with RSA-PSS over
`timestamp_ms + HTTP_METHOD + path` (no query string), SHA-256, sent as three
headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
Public market-data reads need no auth; anything under /portfolio (including
order placement) does.

Docs: https://docs.kalshi.com/getting_started/api_keys
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# trading-api.kalshi.com now answers every request with "API has been moved
# to https://api.elections.kalshi.com/" (confirmed 2026-09-27).
DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class KalshiCredentials:
    api_key_id: str
    private_key_pem: bytes


class KalshiClient:
    def __init__(
        self,
        credentials: KalshiCredentials | None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
    ):
        self._creds = credentials
        self._base_url = base_url.rstrip("/")
        # Kalshi signs the full request path, e.g. "/trade-api/v2/markets",
        # not just the part after the base URL.
        self._base_path = urlparse(self._base_url).path
        self._timeout = timeout
        self._private_key: rsa.RSAPrivateKey | None = None
        if credentials is not None:
            self._private_key = serialization.load_pem_private_key(
                credentials.private_key_pem, password=None
            )

    def _sign(self, method: str, path: str) -> dict[str, str]:
        if self._creds is None or self._private_key is None:
            raise RuntimeError("Kalshi credentials are required for this request")
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{self._base_path}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._creds.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Content-Type": "application/json"}
        # Sign whenever we have credentials, not only when the endpoint
        # strictly requires it -- market-data reads came back 401 unsigned.
        if auth or self._creds is not None:
            headers.update(self._sign(method, path))
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    # ---- public market data (no auth) ----

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}")

    def list_markets(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/markets", params=params)

    def iter_markets(self, max_pages: int = 10, **params: Any) -> list[dict[str, Any]]:
        """All markets matching params, following Kalshi's `cursor` paging."""
        markets: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            page = self.list_markets(limit=1000, **params, **({"cursor": cursor} if cursor else {}))
            markets.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if not cursor:
                break
        return markets

    def list_series(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/series", params=params)

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}/orderbook")

    # ---- authenticated portfolio / trading ----

    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", auth=True)

    def get_positions(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/portfolio/positions", params=params, auth=True)

    def create_order(
        self,
        *,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str = "buy",
        count: int,
        order_type: str = "market",
        yes_price: int | None = None,
        no_price: int | None = None,
        time_in_force: str | None = None,   # e.g. "immediate_or_cancel"
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if time_in_force is not None:
            body["time_in_force"] = time_in_force
        return self._request("POST", "/portfolio/orders", json_body=body, auth=True)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)
