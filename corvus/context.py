from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4


class ContentOrigin(StrEnum):
    USER = "user"
    REPOSITORY = "repository"
    TOOL = "tool"
    SUBAGENT = "subagent"
    MODEL = "model"
    SYSTEM = "system"
    POLICY = "policy"


class TrustClass(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


class ContextOwnerKind(StrEnum):
    ROOT = "root"
    SUBAGENT = "subagent"
    CONVERSATION = "conversation"
    LEGACY_RUN = "legacy_run"


_EXTERNAL_ORIGINS = frozenset(
    {
        ContentOrigin.USER,
        ContentOrigin.REPOSITORY,
        ContentOrigin.TOOL,
        ContentOrigin.SUBAGENT,
        ContentOrigin.MODEL,
    }
)
_TRUSTED_ORIGINS = frozenset({ContentOrigin.SYSTEM, ContentOrigin.POLICY})
_FIREWALL_POLICY = "corvus-context-firewall:m005-001:v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextOwner:
    kind: ContextOwnerKind
    id: UUID

    @classmethod
    def root(cls, run_id: UUID) -> ContextOwner:
        return cls(kind=ContextOwnerKind.ROOT, id=run_id)

    @classmethod
    def subagent(cls, run_id: UUID) -> ContextOwner:
        return cls(kind=ContextOwnerKind.SUBAGENT, id=run_id)

    @classmethod
    def conversation(cls, conversation_id: UUID) -> ContextOwner:
        return cls(kind=ContextOwnerKind.CONVERSATION, id=conversation_id)

    @classmethod
    def legacy_run(cls, run_id: UUID) -> ContextOwner:
        return cls(kind=ContextOwnerKind.LEGACY_RUN, id=run_id)


@dataclass(frozen=True)
class ContextMessage:
    role: Literal["system", "user"]
    kind: Literal["instruction", "data"]
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"content": self.content, "kind": self.kind, "role": self.role}


@dataclass(frozen=True, init=False)
class ExternalContent:
    id: UUID
    origin: ContentOrigin
    source: str
    trust_class: TrustClass
    content_digest: str
    _content_json: str

    @classmethod
    def _create(
        cls,
        content: Any,
        *,
        origin: ContentOrigin,
        source: str,
        trust_class: TrustClass,
    ) -> ExternalContent:
        if not source or not source.strip():
            raise ValueError("context source must be non-empty")
        if origin in _EXTERNAL_ORIGINS and trust_class is not TrustClass.UNTRUSTED:
            raise ValueError("external content cannot be trusted")
        if origin in _TRUSTED_ORIGINS and trust_class is not TrustClass.TRUSTED:
            raise ValueError("system and policy content must be trusted")
        if origin not in _EXTERNAL_ORIGINS | _TRUSTED_ORIGINS:
            raise ValueError(f"unsupported context origin: {origin}")
        if origin in _TRUSTED_ORIGINS and not isinstance(content, str):
            raise TypeError("trusted instructions must be strings")
        content_json = _canonical_json(content)
        instance = object.__new__(cls)
        object.__setattr__(instance, "id", uuid4())
        object.__setattr__(instance, "origin", origin)
        object.__setattr__(instance, "source", source)
        object.__setattr__(instance, "trust_class", trust_class)
        object.__setattr__(instance, "_content_json", content_json)
        object.__setattr__(instance, "content_digest", _sha256_text(content_json))
        return instance

    @classmethod
    def user(cls, content: Any, *, source: str) -> ExternalContent:
        return cls._create(
            content,
            origin=ContentOrigin.USER,
            source=source,
            trust_class=TrustClass.UNTRUSTED,
        )

    @classmethod
    def repository(cls, content: Any, *, source: str) -> ExternalContent:
        return cls._create(
            content,
            origin=ContentOrigin.REPOSITORY,
            source=source,
            trust_class=TrustClass.UNTRUSTED,
        )

    @classmethod
    def tool(cls, content: Any, *, source: str) -> ExternalContent:
        return cls._create(
            content,
            origin=ContentOrigin.TOOL,
            source=source,
            trust_class=TrustClass.UNTRUSTED,
        )

    @classmethod
    def subagent(cls, content: Any, *, source: str) -> ExternalContent:
        return cls._create(
            content,
            origin=ContentOrigin.SUBAGENT,
            source=source,
            trust_class=TrustClass.UNTRUSTED,
        )

    @classmethod
    def model(cls, content: Any, *, source: str) -> ExternalContent:
        return cls._create(
            content,
            origin=ContentOrigin.MODEL,
            source=source,
            trust_class=TrustClass.UNTRUSTED,
        )

    @classmethod
    def system(cls, instruction: str, *, source: str = "corvus:system") -> ExternalContent:
        return cls._create(
            instruction,
            origin=ContentOrigin.SYSTEM,
            source=source,
            trust_class=TrustClass.TRUSTED,
        )

    @classmethod
    def policy(cls, instruction: str, *, source: str = "corvus:policy") -> ExternalContent:
        return cls._create(
            instruction,
            origin=ContentOrigin.POLICY,
            source=source,
            trust_class=TrustClass.TRUSTED,
        )

    @property
    def data(self) -> Any:
        return json.loads(self._content_json)

    @property
    def content_json(self) -> str:
        return self._content_json

    @property
    def source_locator_digest(self) -> str:
        return _sha256_text(self.source)


@dataclass(frozen=True)
class ContextEnvelope:
    owner: ContextOwner
    trusted: tuple[ExternalContent, ...] = ()
    external: tuple[ExternalContent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted", tuple(self.trusted))
        object.__setattr__(self, "external", tuple(self.external))
        if any(item.trust_class is not TrustClass.TRUSTED for item in self.trusted):
            raise ValueError("trusted channel accepts only trusted system or policy content")
        if any(item.origin not in _TRUSTED_ORIGINS for item in self.trusted):
            raise ValueError("trusted channel accepts only system or policy content")
        if any(item.trust_class is not TrustClass.UNTRUSTED for item in self.external):
            raise ValueError("external channel accepts only untrusted content")
        if any(item.origin not in _EXTERNAL_ORIGINS for item in self.external):
            raise ValueError("external channel accepts only external content")

    @classmethod
    def compose(
        cls,
        *,
        owner: ContextOwner,
        trusted: tuple[ExternalContent, ...] = (),
        external: tuple[ExternalContent, ...] = (),
    ) -> ContextEnvelope:
        return cls(owner=owner, trusted=tuple(trusted), external=tuple(external))

    def messages(self) -> tuple[ContextMessage, ...]:
        messages: list[ContextMessage] = []
        for item in self.trusted:
            messages.append(ContextMessage(role="system", kind="instruction", content=item.data))
        for item in self.external:
            payload = _canonical_json(
                {
                    "content_digest": item.content_digest,
                    "data": item.data,
                    "origin": item.origin.value,
                    "source": item.source,
                    "trust_class": item.trust_class.value,
                }
            )
            messages.append(ContextMessage(role="user", kind="data", content=payload))
        return tuple(messages)

    @property
    def system_instruction_digest(self) -> str:
        return _sha256_text(_canonical_json([item.content_digest for item in self.trusted]))

    @property
    def firewall_policy_digest(self) -> str:
        return _sha256_text(_FIREWALL_POLICY)

    @property
    def digest(self) -> str:
        payload = _canonical_json(
            {
                "owner": {"id": str(self.owner.id), "kind": self.owner.kind.value},
                "messages": [message.as_dict() for message in self.messages()],
                "firewall_policy_digest": self.firewall_policy_digest,
            }
        )
        return _sha256_text(payload)


class ContextProvenanceSink(Protocol):
    def append_context_envelope(self, envelope: ContextEnvelope) -> UUID: ...

    def append_external_content(self, owner: ContextOwner, content: ExternalContent) -> None: ...
