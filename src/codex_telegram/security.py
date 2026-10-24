"""Authentication is checked on every bot update and Mini App request."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl
from .config import SafeError


def authenticate(init_data: str, bot_token: str, owner_id: int, *, now=None, ttl=600) -> int:
    if not isinstance(init_data, str) or len(init_data) > 16384:
        raise SafeError("Authorization expired. Reopen the panel from Telegram.")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(pairs) != len(data):
        raise SafeError("Invalid authorization.")
    supplied = data.pop("hash", "")
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise SafeError("Invalid authorization.")
    try:
        stamp = int(data["auth_date"])
        current = time.time() if now is None else now
        if not -30 <= current - stamp <= ttl:
            raise ValueError()
        user = json.loads(data["user"])
        if type(user.get("id")) is not int or user["id"] != owner_id:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise SafeError("Authorization expired or access denied.") from None
    return owner_id


def owned_message(message: dict, owner_id: int) -> bool:
    return (message.get("from", {}).get("id") == owner_id
            and message.get("chat", {}).get("id") == owner_id
            and message.get("chat", {}).get("type") == "private")
