import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshibot.kalshi_client import KalshiClient, KalshiCredentials


def test_signature_covers_full_request_path():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    client = KalshiClient(
        KalshiCredentials(api_key_id="k", private_key_pem=pem),
        base_url="https://example.com/trade-api/v2",
    )

    headers = client._sign("get", "/markets")

    # Kalshi verifies timestamp + METHOD + full path including /trade-api/v2.
    expected = f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode()
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        expected,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
