"""FastAPI 应用工厂和依赖生命周期。"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import QdrantClient

from agent.config import ROOT, Settings
from agent.graph import AgentWorkflow
from agent.model_gateway import create_gateway
from agent.observability.tracing import TraceStore
from agent.retriever.ingest import ingest, open_qdrant
from agent.retriever.service import Retriever
from agent.sql.schema_catalog import SchemaCatalog
from agent.tools.registry import ToolClient
from api.routes_chat import router as chat_router
from api.routes_health import router as health_router
from api.routes_metrics import router as metrics_router
from api.routes_traces import router as traces_router
from scripts.verify_data_manifest import Verification


def create_app(
    settings: Settings | None = None,
    *,
    qdrant_client: QdrantClient | None = None,
    tool_client: ToolClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.validate_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.resolved_database_path().resolve() == (ROOT / "database" / "ecommerce.db").resolve():
            verifier = Verification(ROOT)
            if not verifier.run(ROOT / "data_manifest.json"):
                raise RuntimeError("Synthetic data manifest verification failed: " + "; ".join(verifier.errors[:3]))
        store = TraceStore(settings.resolved_state_path(), settings.fernet())
        catalog = SchemaCatalog.from_database(settings.resolved_database_path())
        qdrant = qdrant_client or open_qdrant(settings.qdrant_url)
        ingest(
            qdrant,
            settings.resolved_knowledge_path(),
            settings.resolved_knowledge_path().parent / "ingest_manifest.json",
        )
        tools = tool_client or ToolClient(settings)
        workflow = AgentWorkflow(
            settings, store, Retriever(qdrant, settings.analysis_now().date()), catalog, tools, create_gateway(settings)
        )
        app.state.settings = settings
        app.state.store = store
        app.state.qdrant = qdrant
        app.state.tools = tools
        app.state.workflow = workflow

        async def outbox_loop():
            while True:
                try:
                    await workflow.retry_outbox_once()
                except Exception:
                    pass
                await asyncio.sleep(15)

        worker = asyncio.create_task(outbox_loop())
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await tools.aclose()
            qdrant.close()

    app = FastAPI(title="智能售后数据 Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[item.strip() for item in settings.cors_origins.split(",") if item.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Metrics-Token"],
    )
    app.include_router(chat_router)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(traces_router)
    return app


app = create_app()
