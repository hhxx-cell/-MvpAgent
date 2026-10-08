"""确定性 Markdown 切块与 Qdrant 向量入库。"""

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from qdrant_client import QdrantClient, models

COLLECTION = "aftersales_knowledge"
VECTOR_SIZE = 256
PARAGRAPH = re.compile(r"^\s*\[(p-\d+)\]\s*(.+)$")


def embed(text: str) -> list[float]:
    """可复现的本地字符 n-gram 向量，避免 MVP 依赖外部 embedding 服务。"""
    clean = re.sub(r"\s+", "", text.lower())
    features = [clean[i : i + 2] for i in range(max(0, len(clean) - 1))]
    features += [clean[i : i + 3] for i in range(max(0, len(clean) - 2))]
    values = [0.0] * VECTOR_SIZE
    for feature in features:
        digest = hashlib.sha256(feature.encode()).digest()
        values[int.from_bytes(digest[:2], "big") % VECTOR_SIZE] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def read_document(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {path.name}")
    _, frontmatter, body = raw.split("---", 2)
    metadata = yaml.safe_load(frontmatter) or {}
    required = {"policy_key", "effective_date", "title"}
    if not required.issubset(metadata):
        raise ValueError(f"missing knowledge metadata: {path.name}")
    chunks = []
    section = str(metadata["title"])
    for line in body.splitlines():
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        match = PARAGRAPH.match(line)
        if not match:
            continue
        paragraph_id, content = match.groups()
        chunks.append(
            {
                "source_file": path.name,
                "policy_key": str(metadata["policy_key"]),
                "effective_date": str(metadata["effective_date"]),
                "version": int(metadata.get("version", 0)),
                "title": str(metadata["title"]),
                "category": str(metadata.get("category", "售后政策")),
                "section_path": section,
                "paragraph_id": paragraph_id,
                "content": content.strip(),
                "content_hash": hashlib.sha256(content.strip().encode()).hexdigest(),
            }
        )
    if not chunks:
        raise ValueError(f"no numbered paragraphs: {path.name}")
    return chunks


def open_qdrant(url: str) -> QdrantClient:
    return QdrantClient(":memory:") if url == ":memory:" else QdrantClient(url=url, timeout=5)


def ingest(client: QdrantClient, source_dir: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    files = sorted(source_dir.glob("*.md"))
    if not files:
        raise ValueError("knowledge source is empty")
    chunks: list[dict[str, Any]] = []
    quarantined: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        try:
            for chunk in read_document(path):
                key = (chunk["policy_key"], chunk["effective_date"], chunk["content_hash"])
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(chunk)
        except (ValueError, yaml.YAMLError):
            quarantined.append(path.name)
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION, vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE)
    )
    points = [
        models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['source_file']}:{chunk['paragraph_id']}")),
            vector=embed(chunk["title"] + chunk["content"]),
            payload=chunk,
        )
        for chunk in chunks
    ]
    for start in range(0, len(points), 64):
        client.upsert(COLLECTION, points=points[start : start + 64], wait=True)
    manifest = {
        "documents": len(files),
        "chunks": len(chunks),
        "quarantined": quarantined,
        "collection": COLLECTION,
        "embedding": "deterministic-char-ngram-v1",
    }
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
