"""手动重建 Qdrant 知识索引。"""

from agent.config import Settings
from agent.retriever.ingest import ingest, open_qdrant

if __name__ == "__main__":
    settings = Settings()
    result = ingest(
        open_qdrant(settings.qdrant_url),
        settings.resolved_knowledge_path(),
        settings.resolved_knowledge_path().parent / "ingest_manifest.json",
    )
    print(result)
