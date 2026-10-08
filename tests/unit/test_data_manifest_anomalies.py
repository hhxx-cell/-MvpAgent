"""The manifest lists every rejected synthetic order by stable source ID."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from scripts.generate_seed_data import DEFAULT_SEED, generate
from scripts.verify_data_manifest import Verification


def _generated_root(tmp_path: Path) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    for name in ("schema.sql", "schema_catalog.yaml"):
        shutil.copy2(project_root / "database" / name, database_dir / name)
    generate(tmp_path, DEFAULT_SEED)
    return tmp_path


def test_anomaly_samples_are_complete_and_verifiable(tmp_path: Path) -> None:
    root = _generated_root(tmp_path)
    manifest = json.loads((root / "data_manifest.json").read_text(encoding="utf-8"))
    samples = manifest["anomaly_samples"]

    assert len(samples) == manifest["rejected_orders"] == 10
    assert [item["source_line"] for item in samples] == list(range(191, 201))
    assert len({item["sample_id"] for item in samples}) == 10
    assert samples[2] == {
        "sample_id": "orders.jsonl:193",
        "source_line": 193,
        "order_id": None,
        "reason": "missing_required_field:order_id",
    }
    verification = Verification(root)
    assert verification.run(root / "data_manifest.json"), verification.errors


@pytest.mark.parametrize("mutation", ["missing", "wrong_reason", "wrong_order_id"])
def test_manifest_verifier_rejects_anomaly_list_drift(tmp_path: Path, mutation: str) -> None:
    root = _generated_root(tmp_path)
    manifest_path = root / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    modified = copy.deepcopy(manifest)
    if mutation == "missing":
        del modified["anomaly_samples"]
    elif mutation == "wrong_reason":
        modified["anomaly_samples"][0]["reason"] = "unknown_status"
    else:
        modified["anomaly_samples"][0]["order_id"] = "O99999"
    manifest_path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")

    verification = Verification(root)
    assert not verification.run(manifest_path)
    assert "Manifest anomaly_samples differ from rejected_orders.jsonl" in verification.errors
