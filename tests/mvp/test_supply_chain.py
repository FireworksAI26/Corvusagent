from __future__ import annotations

from pathlib import Path

from scripts.generate_supply_chain import build_provenance, build_sbom, build_static_manifest

ROOT = Path(__file__).resolve().parents[2]


def test_sbom_is_reproducible_and_lists_locked_python_components() -> None:
    first = build_sbom(ROOT / "uv.lock")
    second = build_sbom(ROOT / "uv.lock")

    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    names = {component["name"] for component in first["components"]}
    assert {"corvus", "cryptography", "sqlalchemy"}.issubset(names)


def test_provenance_binds_source_commit_build_inputs_and_artifacts(tmp_path: Path) -> None:
    wheel = tmp_path / "corvus.whl"
    static_manifest = tmp_path / "static-manifest.json"
    wheel.write_bytes(b"wheel")
    static_manifest.write_text('{"assets": []}\n', encoding="utf-8")

    provenance = build_provenance(
        ROOT,
        commit="test-commit",
        artifacts=(wheel, static_manifest),
    )

    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    assert [subject["name"] for subject in provenance["subject"]] == [
        "corvus-source",
        "corvus.whl",
        "static-manifest.json",
    ]
    assert all(len(subject["digest"]["sha256"]) == 64 for subject in provenance["subject"])
    assert provenance["predicate"]["vcs"]["commit"] == "test-commit"
    materials = provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
    assert {item["uri"] for item in materials} == {"pyproject.toml", "uv.lock"}


def test_static_manifest_is_sorted_and_content_bound(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("corvus", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("ready", encoding="utf-8")

    manifest = build_static_manifest(tmp_path)

    assert [asset["path"] for asset in manifest["assets"]] == ["assets/app.js", "index.html"]
    assert all(len(asset["sha256"]) == 64 for asset in manifest["assets"])
