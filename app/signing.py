import hashlib
import hmac


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return (
        "v1="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )
