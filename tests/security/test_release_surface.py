from __future__ import annotations

import json
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release.yml"
SECURITY_SCAN_WORKFLOW = ROOT / ".github" / "workflows" / "security-scan.yml"
VERCEL_CONFIG = ROOT / "apps" / "web" / "vercel.json"
HOSTED_RUNTIME_POLICY = ROOT / "apps" / "web" / "HOSTED_RUNTIME_SECURITY.md"


def _workflow_trigger_block() -> str:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    return workflow.split("\non:\n", maxsplit=1)[1].split("\njobs:\n", maxsplit=1)[0]


def _strip_yaml_run_prefix(line: str) -> str:
    stripped_line = line.strip()
    for prefix in ("- run:", "run:"):
        if stripped_line.startswith(prefix):
            return stripped_line.removeprefix(prefix).strip()
    return stripped_line


def _semgrep_command_arguments(workflow: str | None = None) -> list[str]:
    workflow_text = (
        workflow if workflow is not None else SECURITY_SCAN_WORKFLOW.read_text(encoding="utf-8")
    )
    command_line = next(
        (
            candidate
            for line in workflow_text.splitlines()
            if (candidate := _strip_yaml_run_prefix(line)).startswith("semgrep ")
        ),
        None,
    )
    if command_line is None:
        raise AssertionError("security workflow must contain a Semgrep command")
    return shlex.split(command_line)


def test_desktop_packaging_cannot_be_started_by_pull_requests() -> None:
    triggers = _workflow_trigger_block()

    assert "  pull_request:" not in triggers
    assert "  pull_request_target:" not in triggers
    assert "  workflow_dispatch:" in triggers
    assert '      - "v*"' in triggers


def test_semgrep_scans_repository_and_writes_json_artifact() -> None:
    arguments = _semgrep_command_arguments()

    assert arguments[0] == "semgrep"
    assert "." in arguments
    assert "--json-output=semgrep.json" in arguments
    assert "--json" not in arguments
    assert "semgrep.json" not in arguments


def test_semgrep_parser_accepts_inline_yaml_run_forms() -> None:
    command = "semgrep --json-output=semgrep.json ."
    expected_arguments = shlex.split(command)

    assert _semgrep_command_arguments(f"run: {command}") == expected_arguments
    assert _semgrep_command_arguments(f"- run: {command}") == expected_arguments


def test_security_scan_uses_node24_actions() -> None:
    workflow = SECURITY_SCAN_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("actions/checkout@v6") == 2
    assert "gitleaks/gitleaks-action@v3" in workflow
    assert "gitleaks/gitleaks-action@v2" not in workflow


def test_semgrep_failures_are_not_silenced() -> None:
    workflow = SECURITY_SCAN_WORKFLOW.read_text(encoding="utf-8")
    semgrep_line = next(
        line.strip() for line in workflow.splitlines() if line.lstrip().startswith("semgrep ")
    )

    assert "|| true" not in semgrep_line


def test_hosted_alpha_keeps_a_documented_same_origin_network_policy() -> None:
    assert HOSTED_RUNTIME_POLICY.exists(), "hosted runtime network policy must be documented"

    policy = HOSTED_RUNTIME_POLICY.read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())
    vercel = json.loads(VERCEL_CONFIG.read_text(encoding="utf-8"))
    content_security_policy = next(
        header["value"]
        for rule in vercel["headers"]
        for header in rule["headers"]
        if header["key"] == "Content-Security-Policy"
    )

    assert "connect-src 'self'" in content_security_policy
    assert "No external streaming or API origin is required for the alpha" in normalized_policy
    assert "unverified trust boundary" in normalized_policy
