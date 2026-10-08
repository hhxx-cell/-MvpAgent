"""按政策类别检索并只引用最新有效版本。"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from qdrant_client import QdrantClient, models

from agent.guards.prompt_injection import contains_instruction
from agent.retriever.ingest import COLLECTION, embed
from agent.retriever.versioning import current_version


@dataclass(frozen=True)
class RetrievalResult:
    answer: str
    citations: list[dict[str, str]]
    warnings: list[str]
    evidence: list[dict[str, Any]]


class Retriever:
    def __init__(self, client: QdrantClient, as_of: date | None = None):
        self.client = client
        self.as_of = as_of

    def answer(self, question: str, policy_key: str) -> RetrievalResult:
        response = self.client.query_points(
            collection_name=COLLECTION,
            query=embed(question),
            query_filter=models.Filter(
                must=[models.FieldCondition(key="policy_key", match=models.MatchValue(value=policy_key))]
            ),
            limit=30,
            with_payload=True,
        )
        candidates = [dict(point.payload or {}, score=point.score) for point in response.points]
        if not candidates:
            return RetrievalResult("知识库中没有足够依据。", [], ["RAG_NO_EVIDENCE"], [])
        current, had_older, ambiguous = current_version(candidates, self.as_of)
        if ambiguous:
            return RetrievalResult("当前政策存在无法裁决的版本冲突，请人工确认。", [], ["RAG_VERSION_CONFLICT"], [])
        if not current:
            return RetrievalResult("知识库中没有已生效的依据。", [], ["RAG_NO_EVIDENCE"], [])
        usable = [
            item
            for item in sorted(current, key=lambda item: item["score"], reverse=True)
            if not contains_instruction(item["content"])
        ]
        if not usable:
            return RetrievalResult("知识库中没有足够可信依据。", [], ["RAG_NO_EVIDENCE"], [])
        core = next((item for item in usable if item["paragraph_id"] == "p-002"), None)
        chosen = ([core] if core else []) + [item for item in usable if item is not core][: (1 if core else 2)]
        citations = [
            {
                "source_file": item["source_file"],
                "paragraph_id": item["paragraph_id"],
                "effective_date": str(item["effective_date"]),
            }
            for item in chosen
        ]
        answer = "；".join(item["content"].rstrip("。") for item in chosen) + "。"
        answer += " 来源：" + "、".join(
            f"{item['source_file']} [{item['paragraph_id']}]（生效日期 {item['effective_date']}）" for item in chosen
        )
        if had_older:
            answer += "。知识库存在旧版本，以上采用最新已生效版本。"
        warnings = ["KNOWLEDGE_INJECTION_IGNORED"] if len(usable) < len(current) else []
        return RetrievalResult(answer, citations, warnings, chosen)
