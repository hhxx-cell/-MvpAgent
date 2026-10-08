"""Keep runtime secrets and caches out of the portable source archive."""

import tarfile

from scripts import build_offline_bundle


def test_source_archive_contains_offline_files_but_no_runtime_secrets(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    (root / "scripts" / "__pycache__").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "scripts" / "install_offline_bundle.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "scripts" / ".env.local").write_text("secret", encoding="utf-8")
    (root / "scripts" / "__pycache__" / "cache.pyc").write_bytes(b"cache")
    (root / "runtime" / "secret.txt").write_text("secret", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    monkeypatch.setattr(build_offline_bundle, "ROOT", root)

    archive_path = tmp_path / "source.tar.gz"
    build_offline_bundle.build_source_archive(archive_path)
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())

    assert "scripts/install_offline_bundle.sh" in names
    assert ".env.example" in names
    assert "scripts/.env.local" not in names
    assert ".env" not in names
    assert not any("runtime" in name or "__pycache__" in name or name.endswith(".pyc") for name in names)
