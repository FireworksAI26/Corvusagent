from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class OAuthError(RuntimeError):
    """Stable, value-free OAuth failure."""


@dataclass(frozen=True, slots=True)
class OAuthStart:
    authorization_url: str


@dataclass(frozen=True, slots=True)
class OAuthCallback:
    code: str
    state: str


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str


class OAuthClient(Protocol):
    def start(self, redirect_uri: str) -> OAuthStart: ...

    def exchange(self, callback: OAuthCallback) -> VerifiedIdentity: ...

    def abort(self, state: str) -> None: ...
