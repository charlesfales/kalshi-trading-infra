"""RSA-PSS request signing for the Kalshi REST API (v2).

Kalshi authenticates each request by signing ``timestamp_ms + METHOD + path``, where
``path`` includes the full ``/trade-api/v2`` prefix and excludes the query string.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class Signer:
    def __init__(self, api_key_id: str, private_key_path: str | Path) -> None:
        if not api_key_id:
            raise ValueError("api_key_id is required")
        p = Path(private_key_path)
        if not p.exists():
            raise FileNotFoundError(f"private key not found at {p}")
        loaded = serialization.load_pem_private_key(p.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError(f"expected an RSA private key, got {type(loaded).__name__}")
        self.api_key_id = api_key_id
        self._key = loaded

    @classmethod
    def from_key(cls, api_key_id: str, key: rsa.RSAPrivateKey) -> Signer:
        """Build from an in-memory key (tests, secret managers)."""
        self = cls.__new__(cls)
        self.api_key_id = api_key_id
        self._key = key
        return self

    def sign(self, message: str) -> str:
        sig = self._key.sign(
            message.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str, *, now_ms: int | None = None) -> dict[str, str]:
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        path = path.split("?", 1)[0]
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(f"{ts}{method.upper()}{path}"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def self_test(self) -> None:
        """Sign and verify a probe message. Run at startup: a bot that cannot sign should
        refuse to start, not discover it on its first order."""
        msg = "self-test"
        sig = base64.b64decode(self.sign(msg))
        self._key.public_key().verify(
            sig, msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
