from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast
from urllib.parse import urlencode

import httpx
from authlib.deprecate import AuthlibDeprecationWarning  # type: ignore[import-untyped]

from corvus.application.oauth import OAuthCallback, OAuthError, OAuthStart, VerifiedIdentity
from corvus.infrastructure.oauth.repository import OAuthTransactionRepository

warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
from authlib.jose import JoseError, jwt  # type: ignore[import-untyped]  # noqa: E402

GOOGLE_AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"  # noqa: S105
GOOGLE_JWKS_ENDPOINT: Final = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUER: Final = "https://accounts.google.com"
_TRANSACTION_TTL: Final = timedelta(minutes=10)
_HTTP_TIMEOUT_SECONDS: Final = 10.0


class GoogleOAuthTransport(Protocol):
    def exchange_code(self, endpoint: str, form: dict[str, str]) -> dict[str, Any]: ...

    def fetch_jwks(self, endpoint: str) -> dict[str, Any]: ...


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


@dataclass(frozen=True, slots=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret_ref: str
    redirect_uris: frozenset[str]
    state_secret: str

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.client_secret_ref.strip():
            raise ValueError("google_oauth_configuration_invalid")
        if len(self.state_secret) < 32:
            raise ValueError("oauth_state_secret_too_short")
        if not self.redirect_uris:
            raise ValueError("oauth_redirect_allowlist_empty")


class HttpxGoogleOAuthTransport:
    def exchange_code(self, endpoint: str, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = httpx.post(endpoint, data=form, timeout=_HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise OAuthError("google_token_exchange_failed") from None
        if not isinstance(payload, dict):
            raise OAuthError("google_token_response_invalid")
        return cast(dict[str, Any], payload)

    def fetch_jwks(self, endpoint: str) -> dict[str, Any]:
        try:
            response = httpx.get(endpoint, timeout=_HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise OAuthError("google_jwks_unavailable") from None
        if not isinstance(payload, dict):
            raise OAuthError("google_jwks_invalid")
        return cast(dict[str, Any], payload)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _valid_state(state: str, secret: str) -> bool:
    try:
        payload = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False
    if len(payload) != 64 or not hmac.compare_digest(_b64url(payload), state):
        return False
    random_state, supplied_mac = payload[:32], payload[32:]
    expected_mac = hmac.digest(secret.encode(), random_state, "sha256")
    return hmac.compare_digest(supplied_mac, expected_mac)


def _header(id_token: str) -> Mapping[str, Any]:
    try:
        encoded = id_token.split(".", maxsplit=1)[0]
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, json.JSONDecodeError):
        raise OAuthError("google_id_token_malformed") from None
    if not isinstance(payload, dict):
        raise OAuthError("google_id_token_malformed")
    return cast(Mapping[str, Any], payload)


class GoogleOAuthClient:
    authorization_endpoint = GOOGLE_AUTHORIZATION_ENDPOINT

    def __init__(
        self,
        *,
        config: GoogleOAuthConfig,
        transactions: OAuthTransactionRepository,
        transport: GoogleOAuthTransport,
        secret_resolver: SecretResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.transactions = transactions
        self.transport = transport
        self.secret_resolver = secret_resolver
        self.clock = clock

    def start(self, redirect_uri: str) -> OAuthStart:
        if redirect_uri not in self.config.redirect_uris:
            raise OAuthError("oauth_redirect_uri_forbidden")
        now = self.clock()
        random_state = secrets.token_bytes(32)
        state_mac = hmac.digest(self.config.state_secret.encode(), random_state, "sha256")
        state = _b64url(random_state + state_mac)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        self.transactions.create(
            state=state,
            nonce=nonce,
            redirect_uri=redirect_uri,
            pkce_verifier=verifier,
            created_at=now,
            expires_at=now + _TRANSACTION_TTL,
        )
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return OAuthStart(authorization_url=f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}")

    def exchange(self, callback: OAuthCallback) -> VerifiedIdentity:
        if not callback.code or not callback.state:
            raise OAuthError("oauth_callback_invalid")
        if not _valid_state(callback.state, self.config.state_secret):
            raise OAuthError("oauth_state_invalid")
        now = self.clock()
        transaction = self.transactions.consume(state=callback.state, now=now)
        client_secret = self.secret_resolver.resolve(self.config.client_secret_ref)
        if not client_secret:
            raise OAuthError("google_client_secret_unavailable")
        token_response = self.transport.exchange_code(
            GOOGLE_TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "code": callback.code,
                "client_id": self.config.client_id,
                "client_secret": client_secret,
                "redirect_uri": transaction.redirect_uri,
                "code_verifier": transaction.pkce_verifier,
            },
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OAuthError("google_id_token_missing")
        claims = self._verify_id_token(id_token, now=now)
        if claims.get("nonce") != transaction.nonce:
            raise OAuthError("google_id_token_nonce_invalid")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise OAuthError("google_id_token_subject_invalid")
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise OAuthError("google_email_invalid")
        if claims.get("email_verified") is not True:
            raise OAuthError("google_email_unverified")
        display_name = claims.get("name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = email.split("@", maxsplit=1)[0]
        return VerifiedIdentity(
            issuer=GOOGLE_ISSUER,
            subject=subject,
            email=email,
            email_verified=True,
            display_name=display_name,
        )

    def abort(self, state: str) -> None:
        if not state or not _valid_state(state, self.config.state_secret):
            raise OAuthError("oauth_state_invalid")
        self.transactions.consume(state=state, now=self.clock())

    def _verify_id_token(self, id_token: str, *, now: datetime) -> Mapping[str, Any]:
        header = _header(id_token)
        if header.get("alg") != "RS256":
            raise OAuthError("google_id_token_algorithm_invalid")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OAuthError("google_id_token_key_invalid")
        jwks = self.transport.fetch_jwks(GOOGLE_JWKS_ENDPOINT)
        key = self._key_by_id(jwks, kid)
        if key is None:
            jwks = self.transport.fetch_jwks(GOOGLE_JWKS_ENDPOINT)
            key = self._key_by_id(jwks, kid)
        if key is None:
            raise OAuthError("google_id_token_key_unavailable")
        try:
            decoded = jwt.decode(id_token, key)
        except JoseError:
            raise OAuthError("google_id_token_signature_invalid") from None
        claims = cast(Mapping[str, Any], decoded)
        if claims.get("iss") != GOOGLE_ISSUER:
            raise OAuthError("google_id_token_issuer_invalid")
        audience = claims.get("aud")
        if not (
            audience == self.config.client_id
            or isinstance(audience, list)
            and self.config.client_id in audience
        ):
            raise OAuthError("google_id_token_audience_invalid")
        azp = claims.get("azp")
        if azp is not None and azp != self.config.client_id:
            raise OAuthError("google_id_token_authorized_party_invalid")
        now_timestamp = int(now.timestamp())
        exp = claims.get("exp")
        iat = claims.get("iat")
        nbf = claims.get("nbf")
        if not isinstance(exp, int) or exp <= now_timestamp:
            raise OAuthError("google_id_token_expired")
        if not isinstance(iat, int) or iat > now_timestamp:
            raise OAuthError("google_id_token_issued_at_invalid")
        if nbf is not None and (not isinstance(nbf, int) or nbf > now_timestamp):
            raise OAuthError("google_id_token_not_yet_valid")
        return claims

    @staticmethod
    def _key_by_id(jwks: Mapping[str, Any], kid: str) -> Mapping[str, Any] | None:
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise OAuthError("google_jwks_invalid")
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid and key.get("alg") == "RS256":
                return cast(Mapping[str, Any], key)
        return None
