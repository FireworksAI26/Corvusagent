from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Final, Literal, TypedDict

from corvus.mvp.store import SqliteStore

ProviderPreference = Literal[
    "codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter",
    "together", "fireworks", "groq", "mistral", "deepseek"
]
EffortPreference = Literal["low", "medium", "high", "xhigh", "max"]
ModePreference = Literal["chat", "build"]
ResponseTone = Literal["concise", "balanced", "detailed"]

DEFAULT_PROVIDER: Final[ProviderPreference] = "codex"
DEFAULT_EFFORT: Final[EffortPreference] = "medium"
DEFAULT_MODE: Final[ModePreference] = "chat"
DEFAULT_RESPONSE_TONE: Final[ResponseTone] = "balanced"


class LocalPreferences(TypedDict):
    version: int
    default_provider: ProviderPreference
    default_model: str | None
    model_labels: dict[str, str]
    default_effort: EffortPreference
    default_mode: ModePreference
    mcp_enabled: bool
    response_tone: ResponseTone
    custom_rules: str
    updated_at: str | None


class LocalPreferencesConflict(RuntimeError):
    def __init__(self, current: LocalPreferences) -> None:
        super().__init__("preferences_version_conflict")
        self.current = current


def default_local_preferences() -> LocalPreferences:
    return {
        "version": 0,
        "default_provider": DEFAULT_PROVIDER,
        "default_model": None,
        "model_labels": {},
        "default_effort": DEFAULT_EFFORT,
        "default_mode": DEFAULT_MODE,
        "mcp_enabled": False,
        "response_tone": DEFAULT_RESPONSE_TONE,
        "custom_rules": "",
        "updated_at": None,
    }


class LocalPreferencesService:
    """Persist non-secret, owner-scoped runtime defaults for the local app."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def get(self, user_id: str) -> LocalPreferences:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_local_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return default_local_preferences() if row is None else self._from_row(row)

    def update(
        self,
        *,
        user_id: str,
        expected_version: int,
        default_provider: ProviderPreference,
        default_model: str | None,
        model_labels: dict[str, str],
        default_effort: EffortPreference,
        default_mode: ModePreference,
        mcp_enabled: bool,
        response_tone: ResponseTone,
        custom_rules: str,
    ) -> LocalPreferences:
        updated_at = datetime.now(UTC).isoformat()
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_local_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current = default_local_preferences() if row is None else self._from_row(row)
            if current["version"] != expected_version:
                raise LocalPreferencesConflict(current)
            version = expected_version + 1
            values = (
                version,
                default_provider,
                default_model,
                json.dumps(model_labels, sort_keys=True, separators=(",", ":")),
                default_effort,
                default_mode,
                int(mcp_enabled),
                response_tone,
                custom_rules,
                updated_at,
                user_id,
            )
            if row is None:
                connection.execute(
                    "INSERT INTO mvp_local_preferences "
                    "(version, default_provider, default_model, model_labels_json, default_effort, "
                    "default_mode, mcp_enabled, response_tone, custom_rules, updated_at, user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            else:
                result = connection.execute(
                    "UPDATE mvp_local_preferences SET version = ?, default_provider = ?, "
                    "default_model = ?, model_labels_json = ?, default_effort = ?, "
                    "default_mode = ?, mcp_enabled = ?, "
                    "response_tone = ?, custom_rules = ?, updated_at = ? "
                    "WHERE user_id = ? AND version = ?",
                    (*values, expected_version),
                )
                if result.rowcount != 1:
                    latest = connection.execute(
                        "SELECT * FROM mvp_local_preferences WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    raise LocalPreferencesConflict(
                        default_local_preferences() if latest is None else self._from_row(latest)
                    )
        return {
            "version": version,
            "default_provider": default_provider,
            "default_model": default_model,
            "model_labels": dict(model_labels),
            "default_effort": default_effort,
            "default_mode": default_mode,
            "mcp_enabled": mcp_enabled,
            "response_tone": response_tone,
            "custom_rules": custom_rules,
            "updated_at": updated_at,
        }

    @staticmethod
    def _from_row(row: Row) -> LocalPreferences:
        return {
            "version": int(row["version"]),
            "default_provider": row["default_provider"],
            "default_model": row["default_model"],
            "model_labels": json.loads(str(row["model_labels_json"])),
            "default_effort": row["default_effort"],
            "default_mode": row["default_mode"],
            "mcp_enabled": bool(row["mcp_enabled"]),
            "response_tone": row["response_tone"],
            "custom_rules": str(row["custom_rules"]),
            "updated_at": str(row["updated_at"]),
        }
