from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

_MATERIALS = ("pyproject.toml", "uv.lock")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_sbom(lock_path: Path) -> dict[str, Any]:
    with lock_path.open("rb") as source:
        lock = tomllib.load(source)
    components = []
    for package in lock.get("package", []):
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name}@{version}",
            }
        )
    components.sort(key=lambda item: (item["name"], item["version"]))
    corvus = next(
        (component for component in components if component["name"] == "corvus"),
        {"type": "application", "name": "corvus", "version": "unknown"},
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {**corvus, "type": "application"}},
        "components": components,
    }


def build_static_manifest(static_dir: Path) -> dict[str, Any]:
    root = static_dir.resolve()
    if not (root / "index.html").is_file():
        raise ValueError("static_web_index_missing")
    assets: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise ValueError(f"static_asset_outside_root:{path}")
        assets.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return {"format": "corvus-static-assets-v1", "assets": assets}


def build_provenance(
    root: Path,
    *,
    commit: str,
    artifacts: tuple[Path, ...] = (),
) -> dict[str, Any]:
    materials: list[dict[str, Any]] = [
        {"uri": name, "digest": {"sha256": _sha256_file(root / name)}} for name in _MATERIALS
    ]
    source_digest = hashlib.sha256(
        "".join(item["digest"]["sha256"] for item in materials).encode("ascii")
    ).hexdigest()
    subjects = [{"name": "corvus-source", "digest": {"sha256": source_digest}}]
    artifact_names: set[str] = set()
    for artifact in sorted(artifacts, key=lambda path: path.name):
        if not artifact.is_file():
            raise ValueError(f"artifact_missing:{artifact}")
        if artifact.name in artifact_names:
            raise ValueError(f"artifact_name_duplicate:{artifact.name}")
        artifact_names.add(artifact.name)
        subjects.append({"name": artifact.name, "digest": {"sha256": _sha256_file(artifact)}})
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://corvus.local/build/python-wheel-v1",
                "externalParameters": {},
                "internalParameters": {},
                "resolvedDependencies": materials,
            },
            "runDetails": {"builder": {"id": "corvus-local-reproducible-builder"}},
            "vcs": {"commit": commit},
        },
    }


def _git_commit(root: Path) -> str:
    git_path = root / ".git"
    if git_path.is_file():
        marker = git_path.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            raise RuntimeError("git_directory_pointer_invalid")
        git_path = (root / marker.removeprefix("gitdir: ")).resolve()
    head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    ref = head.removeprefix("ref: ")
    common_dir = git_path
    common_dir_marker = git_path / "commondir"
    if common_dir_marker.is_file():
        common_dir = (git_path / common_dir_marker.read_text(encoding="utf-8").strip()).resolve()
    for candidate in (git_path / ref, common_dir / ref):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    packed_refs = common_dir / "packed-refs"
    if packed_refs.is_file():
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            commit, packed_ref = line.split(" ", 1)
            if packed_ref == ref:
                return commit
    raise RuntimeError("git_reference_not_found")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic Corvus SBOM and provenance"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("dist/supply-chain"))
    parser.add_argument("--commit")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="Built artifact to bind into provenance; may be repeated",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        help="Built web directory to inventory and bind into provenance",
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    output_dir = arguments.output_dir
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    sbom = build_sbom(root / "uv.lock")
    artifacts = [
        path.resolve() if path.is_absolute() else (root / path).resolve()
        for path in arguments.artifact
    ]
    if arguments.static_dir is not None:
        static_dir = (
            arguments.static_dir.resolve()
            if arguments.static_dir.is_absolute()
            else (root / arguments.static_dir).resolve()
        )
        static_manifest_path = output_dir / "static-manifest.json"
        static_manifest_path.write_text(
            json.dumps(build_static_manifest(static_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts.append(static_manifest_path)
    provenance = build_provenance(
        root,
        commit=arguments.commit or _git_commit(root),
        artifacts=tuple(artifacts),
    )
    (output_dir / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "provenance.intoto.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
