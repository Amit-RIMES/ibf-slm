import hashlib
import hmac

from app.core.config import settings


def _token_for(jwt_token: str) -> str:
    """Derive a CSRF token from the session JWT using HMAC-SHA256."""
    return hmac.new(settings.SECRET_KEY.encode(), jwt_token.encode(), hashlib.sha256).hexdigest()


def validate_csrf(jwt_token: str, submitted: str) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(_token_for(jwt_token), submitted)
