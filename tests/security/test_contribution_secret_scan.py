from __future__ import annotations

from pathlib import Path

import pytest

from corvus.mvp.secret_scan import SecretScanError, SecretScanner


def test_executed_clean_scan_is_the_only_way_to_return_passed(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("ordinary content\n", encoding="utf-8")
    scanner = SecretScanner()

    pending = scanner.not_scanned(("safe.txt",))
    completed = scanner.scan(tmp_path, ("safe.txt",))

    assert pending.status == "not_scanned"
    assert pending.completed_at is None
    assert pending.digest is None
    assert completed.status == "passed"
    assert completed.completed_at is not None
    assert completed.digest is not None


def test_known_tokens_are_blocked_without_echoing_secret(tmp_path: Path) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"  # noqa: S105 - synthetic scanner fixture
    (tmp_path / "config.env").write_text(f"TOKEN={secret}\n", encoding="utf-8")

    result = SecretScanner().scan(tmp_path, ("config.env",))

    assert result.status == "blocked"
    assert result.findings[0].kind == "github_token"
    assert result.findings[0].path == "config.env"
    assert secret not in result.model_dump_json()


def test_high_entropy_values_warn_and_git_metadata_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "candidate.txt").write_text(
        "session=QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5QUJDREVGRw==\n",
        encoding="utf-8",
    )
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "credentials").write_text("ghp_abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")

    result = SecretScanner().scan(tmp_path, ("candidate.txt",))

    assert result.status == "warning"
    assert all(not finding.path.startswith(".git") for finding in result.findings)


def test_scan_refuses_escape_symlinks_and_binary_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("ghp_abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SecretScanError, match="path_invalid"):
        SecretScanner().scan(tmp_path, ("../outside-secret.txt",))
    with pytest.raises(SecretScanError, match="path_link_forbidden"):
        SecretScanner().scan(tmp_path, ("linked.txt",))

    (tmp_path / "binary.bin").write_bytes(b"\0ghp_abcdefghijklmnopqrstuvwxyz123456")
    binary = SecretScanner().scan(tmp_path, ("binary.bin",))
    assert binary.status == "warning"
    assert binary.findings[0].kind == "binary_not_scanned"


def test_large_file_is_streamed_for_digest_and_only_bounded_content_is_scanned(
    tmp_path: Path,
) -> None:
    target = tmp_path / "generated.txt"
    target.write_bytes(b"ordinary-content\n" * 200_000)

    result = SecretScanner().scan(tmp_path, ("generated.txt",))

    assert result.status == "warning"
    assert result.findings[0].kind == "large_file_not_scanned"
    assert result.digest is not None
