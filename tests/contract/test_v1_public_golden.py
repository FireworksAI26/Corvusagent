from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from typer.main import get_command
from typer.testing import CliRunner

from corvus import __version__
from corvus.cli import app
from corvus.codex_cli import CodexLoginStatus
from corvus.config import CorvusPaths
from corvus.conversations import (
    ConversationEvent,
    ConversationLimits,
    ConversationMessage,
    ConversationSnapshot,
    SubagentPolicy,
)
from corvus.database import V1_REQUIRED_COLUMNS
from corvus.delivery import DeliveryManager
from corvus.memory import MemoryManager
from corvus.models import (
    Artifact,
    ArtifactManifest,
    AutonomyLevel,
    Checkpoint,
    DeliveryBundle,
    MemoryRecord,
    ModelProvider,
    Policy,
    RunEvent,
    RunPhase,
    Skill,
    SkillVersion,
)
from corvus.onboarding import OnboardingChoices, OnboardingState
from corvus.store import TraceStore
from tests.fixture_corpus import verify_v1_fixture_corpus

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v1"
CONTRACT_PATH = FIXTURE_ROOT / "public_contract.json"
_FIXED_BACKUP_KEY = base64.urlsafe_b64encode(b"0" * 32)

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_DIGEST_RE = re.compile(r"\b[0-9a-f]{64}\b")
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
_RICH_BORDER_TRANSLATION = str.maketrans({"╭": "┌", "╮": "┐", "╰": "└", "╯": "┘"})


@dataclass(frozen=True)
class _FakeCodexService:
    executable: Path
    executable_sha256: str = "1" * 64

    async def login_status(self) -> CodexLoginStatus:
        return CodexLoginStatus(ready=True, method="chatgpt", detail="Signed in with ChatGPT.")

    async def login(self) -> CodexLoginStatus:
        return CodexLoginStatus(ready=True, method="chatgpt", detail="Signed in with ChatGPT.")


@dataclass(frozen=True)
class _FakeCodexInstaller:
    install_path: Path
    source: str = "https://example.invalid/codex"
    version: str = "v0.0.0-contract"

    async def install(self, *_: object, **__: object) -> None:
        raise AssertionError("the executable golden must not download or install Codex")


def _fake_launch_tui(*args: object, **kwargs: object) -> None:
    project = args[3] if len(args) > 3 else None
    project_text = project.as_posix() if isinstance(project, Path) else str(project)
    print(
        json.dumps(
            {
                "boundary": "launch_tui",
                "max_subagents": kwargs.get("max_subagents"),
                "project": project_text,
                "sandbox": kwargs.get("sandbox"),
                "subagents": kwargs.get("allow_subagents"),
            },
            default=str,
            sort_keys=True,
        )
    )


@contextmanager
def _controlled_boundaries(corvus_home: Path) -> Iterator[None]:
    service = _FakeCodexService(corvus_home / "bin" / "codex")
    installer = _FakeCodexInstaller(corvus_home / "managed" / "codex")
    with ExitStack() as stack:
        stack.enter_context(
            patch("corvus.cli.DockerSandbox.available", return_value=(False, "docker-unavailable"))
        )
        stack.enter_context(
            patch("corvus.cli.PodmanSandbox.available", return_value=(False, "podman-unavailable"))
        )
        stack.enter_context(patch("corvus.cli.discover_codex_service", return_value=service))
        stack.enter_context(patch("corvus.cli.CodexCliInstaller", return_value=installer))
        stack.enter_context(
            patch("corvus.cli.keyring.get_password", return_value=_FIXED_BACKUP_KEY.decode())
        )
        stack.enter_context(patch("corvus.cli.keyring.set_password", return_value=None))
        stack.enter_context(patch("corvus.cli.launch_tui", side_effect=_fake_launch_tui))
        yield


def _normalize_text(text: str, root: Path) -> str:
    normalized = text.translate(_RICH_BORDER_TRANSLATION).replace("\\", "/")
    normalized = normalized.replace(str(root).replace("\\", "/"), "<corvus-home>")
    normalized = normalized.replace(str(Path.home()).replace("\\", "/"), "<user-home>")
    normalized = _UUID_RE.sub("<uuid>", normalized)
    normalized = _DIGEST_RE.sub("<digest>", normalized)
    normalized = _TIMESTAMP_RE.sub("<timestamp>", normalized)
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
    return normalized.strip()


def _normalize_payload(value: object, root: Path) -> object:
    if isinstance(value, dict):
        return {str(key): _normalize_payload(item, root) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_payload(item, root) for item in value]
    if isinstance(value, str):
        return _normalize_text(value, root)
    if isinstance(value, Enum):
        return _normalize_payload(value.value, root)
    if isinstance(value, Path):
        return _normalize_text(value.as_posix(), root)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _canonicalize_rich_presentation(value: object) -> object:
    if isinstance(value, dict):
        canonical = {key: _canonicalize_rich_presentation(item) for key, item in value.items()}
        if "scenario" in value and isinstance(canonical.get("output"), str):
            canonical["output"] = " ".join(canonical["output"].split())
        return canonical
    if isinstance(value, list):
        return [_canonicalize_rich_presentation(item) for item in value]
    if isinstance(value, str) and any("\u2500" <= character <= "\u257f" for character in value):
        without_borders = "".join(
            " " if "\u2500" <= character <= "\u257f" else character for character in value
        )
        return " ".join(without_borders.split())
    return value


def _normalize_command_payload(value: object, root: Path, argv: tuple[str, ...]) -> object:
    normalized = _normalize_payload(value, root)
    if argv == ("doctor", "--json") and isinstance(normalized, dict):
        normalized["python"] = "<python-version>"
    return normalized


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return value.__class__.__name__


def _command_contract(command: Any, path: tuple[str, ...]) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    for parameter in command.params:
        parameters.append(
            {
                "default": _json_value(parameter.default),
                "multiple": bool(getattr(parameter, "multiple", False)),
                "name": parameter.name,
                "nargs": parameter.nargs,
                "options": sorted(
                    [*getattr(parameter, "opts", ()), *getattr(parameter, "secondary_opts", ())]
                ),
                "required": parameter.required,
                "type": getattr(parameter.type, "name", parameter.type.__class__.__name__),
            }
        )
    contracts = [
        {
            "help": command.help or "",
            "parameters": parameters,
            "path": list(path),
        }
    ]
    if hasattr(command, "commands"):
        for name, child in sorted(command.commands.items()):
            contracts.extend(_command_contract(child, (*path, name)))
    return contracts


def _shape(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _run_command_golden(
    corvus_home: Path,
    *argv: str,
    input_data: str | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    invocation = tuple(argv)
    result = CliRunner().invoke(
        app,
        list(invocation),
        input=input_data,
        env={
            "COLUMNS": "240",
            "CORVUS_HOME": str(corvus_home),
            "LINES": "24",
            "NO_COLOR": "1",
            "TERM": "dumb",
        },
        prog_name="corvus",
    )
    raw_output = result.output or ""
    if "--json" in invocation:
        parsed_output: object
        if raw_output.strip():
            try:
                parsed_output = _normalize_command_payload(
                    json.loads(raw_output), corvus_home, invocation
                )
            except json.JSONDecodeError:
                parsed_output = [
                    _normalize_command_payload(json.loads(raw), corvus_home, invocation)
                    for raw in raw_output.splitlines()
                    if raw.strip()
                ]
        else:
            parsed_output = []
    else:
        parsed_output = _normalize_text(raw_output, corvus_home)
    golden = {
        "command": [_normalize_text(argument, corvus_home) for argument in invocation],
        "exit_code": result.exit_code,
        "output": parsed_output,
        "unhandled_exception": (
            None
            if result.exception is None or isinstance(result.exception, SystemExit)
            else result.exception.__class__.__name__
        ),
    }
    if scenario is not None:
        golden["scenario"] = scenario
    return golden


def _build_trace_subject(corvus_home: Path) -> UUID:
    store = TraceStore(CorvusPaths(corvus_home).db)
    run_id = uuid4()
    store.append(
        run_id,
        "run.created",
        RunPhase.UNDERSTAND,
        {
            "prompt": "golden contract command",
            "project": str(corvus_home / "project"),
            "autonomy": 3,
            "sandbox_requested": "auto",
        },
    )
    store.append(
        run_id,
        "run.blocked",
        RunPhase.BLOCKED,
        {
            "reason": "contract fixture sandbox guard",
            "sandbox_backend": "none",
            "host_writes": False,
        },
    )
    store.engine.dispose()
    return run_id


def _build_delivery_subject(corvus_home: Path) -> DeliveryBundle:
    paths = CorvusPaths(corvus_home)
    paths.ensure()
    destination = corvus_home / "delivery-project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('old')\r\n")
    manager = DeliveryManager(paths.bundles, paths.backups, backup_key=_FIXED_BACKUP_KEY)
    return manager.package(
        uuid4(),
        destination,
        {"src/app.py": b"print('new')\n"},
        {"passed": True},
        {"passed": True},
    )


def _command_help_matrix(corvus_home: Path) -> list[dict[str, Any]]:
    contracts = _command_contract(get_command(app), ("corvus",))
    return [
        _run_command_golden(corvus_home, *contract["path"][1:], "--help") for contract in contracts
    ]


def _command_golden_matrix(corvus_home: Path) -> list[dict[str, Any]]:
    project = corvus_home / "project"
    project.mkdir(parents=True, exist_ok=True)
    eval_suite = corvus_home / "contract-eval.yaml"
    eval_suite.write_text(
        "name: v1-contract\ncases:\n"
        "  - id: no-host-write\n"
        "    prompt: contract evaluation\n"
        "    expect_event: run.blocked\n"
        "    expect_no_host_writes: true\n",
        encoding="utf-8",
    )
    trace_run_id = _build_trace_subject(corvus_home)
    delivery = _build_delivery_subject(corvus_home)
    project_id = UUID("10000000-0000-0000-0000-000000000001")

    goldens = [
        _run_command_golden(corvus_home, scenario="root"),
        _run_command_golden(corvus_home, "--version", scenario="version"),
        _run_command_golden(
            corvus_home,
            "run",
            "--project",
            str(project),
            "--subagents",
            "--max-subagents",
            "2",
            "--sandbox",
            "none",
            scenario="run-controlled-tui-boundary",
        ),
        _run_command_golden(corvus_home, "doctor", "--json", scenario="doctor-json"),
        _run_command_golden(
            corvus_home,
            "chat",
            "--project",
            str(project),
            "--sandbox",
            "none",
            "--json",
            "golden contract prompt",
            scenario="chat-json",
        ),
        _run_command_golden(
            corvus_home,
            "eval",
            str(eval_suite),
            "--json",
            scenario="eval-json",
        ),
        _run_command_golden(
            corvus_home, "trace", "--json", str(trace_run_id), scenario="trace-json"
        ),
        _run_command_golden(
            corvus_home, "review", str(delivery.id), "--json", scenario="review-json"
        ),
        _run_command_golden(
            corvus_home, "review", str(delivery.id), "--approve", scenario="review-apply"
        ),
        _run_command_golden(corvus_home, "undo", str(delivery.id), scenario="undo"),
        _run_command_golden(
            corvus_home,
            "memory",
            "add",
            str(project_id),
            "semantic",
            "contract memory",
            scenario="memory-add",
        ),
    ]

    memory_store = TraceStore(CorvusPaths(corvus_home).db)
    memory_id = MemoryManager(memory_store).list(project_id, "local")[0].id
    memory_store.engine.dispose()
    goldens.extend(
        [
            _run_command_golden(
                corvus_home,
                "memory",
                "edit",
                str(memory_id),
                str(project_id),
                "edited contract memory",
                scenario="memory-edit",
            ),
            _run_command_golden(
                corvus_home,
                "memory",
                "pin",
                str(memory_id),
                str(project_id),
                scenario="memory-pin",
            ),
            _run_command_golden(
                corvus_home, "memory", "list", str(project_id), scenario="memory-list"
            ),
            _run_command_golden(
                corvus_home, "memory", "export", str(project_id), scenario="memory-export"
            ),
            _run_command_golden(
                corvus_home,
                "memory",
                "delete",
                str(memory_id),
                str(project_id),
                scenario="memory-delete",
            ),
            _run_command_golden(
                corvus_home,
                "skills",
                "draft",
                "contract-skill",
                "version one",
                "--permission",
                "project_read",
                scenario="skills-draft-v1",
            ),
            _run_command_golden(
                corvus_home,
                "skills",
                "promote",
                "contract-skill",
                "1",
                "--passed",
                scenario="skills-promote-v1",
            ),
            _run_command_golden(
                corvus_home,
                "skills",
                "draft",
                "contract-skill",
                "version two",
                scenario="skills-draft-v2",
            ),
            _run_command_golden(
                corvus_home,
                "skills",
                "promote",
                "contract-skill",
                "2",
                "--passed",
                scenario="skills-promote-v2",
            ),
            _run_command_golden(
                corvus_home,
                "skills",
                "rollback",
                "contract-skill",
                "1",
                scenario="skills-rollback",
            ),
            _run_command_golden(corvus_home, "skills", "list", scenario="skills-list"),
            _run_command_golden(
                corvus_home, "model", "use", "missing", scenario="model-use-missing"
            ),
            _run_command_golden(
                corvus_home,
                "model",
                "add",
                "contract-provider",
                "--kind",
                "openai_compatible",
                "--base-url",
                "https://example.invalid/v1",
                "--model",
                "contract-model",
                scenario="model-add",
            ),
            _run_command_golden(
                corvus_home,
                "model",
                "secret",
                "contract-provider",
                input_data="contract-secret\ncontract-secret\n",
                scenario="model-secret",
            ),
            _run_command_golden(
                corvus_home, "model", "use", "contract-provider", scenario="model-use"
            ),
            _run_command_golden(corvus_home, "model", "list", scenario="model-list"),
            _run_command_golden(corvus_home, "model", "status", scenario="model-status"),
            _run_command_golden(corvus_home, "model", "login", scenario="model-login"),
            _run_command_golden(
                corvus_home,
                "model",
                "install-codex",
                input_data="n\n",
                scenario="model-install-codex-cancel",
            ),
        ]
    )
    return goldens


def build_public_contract(corvus_home: Path) -> dict[str, object]:
    with _controlled_boundaries(corvus_home):
        doctor_golden = _run_command_golden(corvus_home, "doctor", "--json")
        doctor_json = doctor_golden["output"] if isinstance(doctor_golden["output"], dict) else {}
        command_help_goldens = _command_help_matrix(corvus_home)
        command_goldens = _command_golden_matrix(corvus_home)
    schema_models = (
        Policy,
        OnboardingChoices,
        OnboardingState,
        ModelProvider,
        MemoryRecord,
        SkillVersion,
        Skill,
        ConversationLimits,
        SubagentPolicy,
        ConversationMessage,
        ConversationEvent,
        ConversationSnapshot,
        RunEvent,
        DeliveryBundle,
        Artifact,
        ArtifactManifest,
        Checkpoint,
    )
    return {
        "autonomy": {item.name: item.value for item in AutonomyLevel},
        "commands": _command_contract(get_command(app), ("corvus",)),
        "command_goldens": command_goldens,
        "command_help_goldens": command_help_goldens,
        "database": {
            table: sorted(columns) for table, columns in sorted(V1_REQUIRED_COLUMNS.items())
        },
        "doctor_json_shape": _shape(doctor_json),
        "schemas": {model.__name__: model.model_json_schema() for model in schema_models},
        "version": __version__,
    }


def test_v1_public_contract_matches_hashed_golden(tmp_path: Path) -> None:
    corvus_home = tmp_path / "corvus-home"
    fixture_bytes = CONTRACT_PATH.read_bytes()
    fixture_files = verify_v1_fixture_corpus(FIXTURE_ROOT)

    assert fixture_files["public_contract.json"] == {
        "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "size": len(fixture_bytes),
    }
    expected = json.loads(fixture_bytes)
    actual = build_public_contract(corvus_home)

    expected_help_paths = {
        tuple(contract["path"][1:])
        for contract in actual["commands"]  # type: ignore[index]
    }
    executed_help_paths = {
        tuple(golden["command"][:-1])
        for golden in actual["command_help_goldens"]  # type: ignore[index]
    }
    assert executed_help_paths == expected_help_paths
    assert all(
        golden["exit_code"] == 0 and golden["unhandled_exception"] is None
        for golden in actual["command_help_goldens"]  # type: ignore[index]
    )
    required_scenarios = {
        "root",
        "version",
        "run-controlled-tui-boundary",
        "doctor-json",
        "chat-json",
        "eval-json",
        "trace-json",
        "review-json",
        "review-apply",
        "undo",
        "memory-add",
        "memory-edit",
        "memory-pin",
        "memory-list",
        "memory-export",
        "memory-delete",
        "skills-draft-v1",
        "skills-promote-v1",
        "skills-draft-v2",
        "skills-promote-v2",
        "skills-rollback",
        "skills-list",
        "model-use-missing",
        "model-add",
        "model-secret",
        "model-use",
        "model-list",
        "model-status",
        "model-login",
        "model-install-codex-cancel",
    }
    assert {golden["scenario"] for golden in actual["command_goldens"]} == required_scenarios  # type: ignore[index]
    expected_nonzero = {
        "model-install-codex-cancel": 1,
        "model-use-missing": 2,
    }
    assert all(
        golden["exit_code"] == expected_nonzero.get(golden["scenario"], 0)
        and golden["unhandled_exception"] is None
        for golden in actual["command_goldens"]  # type: ignore[index]
    )
    canonical_actual = _canonicalize_rich_presentation(actual)
    canonical_expected = _canonicalize_rich_presentation(expected)
    assert isinstance(canonical_actual, dict)
    assert isinstance(canonical_expected, dict)
    assert canonical_actual.keys() == canonical_expected.keys()

    for key in canonical_actual.keys() - {"command_goldens"}:
        assert canonical_actual[key] == canonical_expected[key], key

    actual_goldens = canonical_actual["command_goldens"]
    expected_goldens = canonical_expected["command_goldens"]
    assert isinstance(actual_goldens, list)
    assert isinstance(expected_goldens, list)
    assert [item["scenario"] for item in actual_goldens] == [
        item["scenario"] for item in expected_goldens
    ]
    for actual_golden, expected_golden in zip(actual_goldens, expected_goldens, strict=True):
        assert actual_golden == expected_golden, actual_golden["scenario"]


def test_rich_help_layout_is_canonical_across_platforms() -> None:
    rounded = {"help": "╭────╮\n│ option value │\n╰────╯"}
    square = {"help": "┌────────┐\n│  option   value  │\n└────────┘"}

    assert _canonicalize_rich_presentation(rounded) == _canonicalize_rich_presentation(square)

    wrapped = {"scenario": "error", "output": "Unable to load\nconfigured provider"}
    unwrapped = {"scenario": "error", "output": "Unable to load configured provider"}
    assert _canonicalize_rich_presentation(wrapped) == _canonicalize_rich_presentation(unwrapped)
