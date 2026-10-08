"""存活与就绪检查。"""

import sqlite3

from fastapi import APIRouter, Request, Response

from agent.retriever.ingest import COLLECTION

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response, readiness: bool = False) -> dict:
    settings = request.app.state.settings
    components = {"database": "down", "vector": "down", "mock": "down", "state": "down"}
    try:
        path = settings.resolved_database_path().resolve()
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("SELECT 1 FROM orders LIMIT 1").fetchone()
        components["database"] = "up"
    except (sqlite3.Error, OSError):
        pass
    try:
        components["vector"] = "up" if request.app.state.qdrant.collection_exists(COLLECTION) else "down"
    except Exception:
        pass
    try:
        request.app.state.store.get_trace("healthcheck")
        components["state"] = "up"
    except Exception:
        pass
    try:
        result = await request.app.state.tools.client.get("/health", timeout=1.0)
        if result.status_code == 200:
            components["mock"] = "up"
    except Exception:
        pass
    ready = all(value == "up" for value in components.values())
    if readiness and not ready:
        response.status_code = 503
    return {"status": "ok" if ready else "degraded", "ready": ready, "components": components}
