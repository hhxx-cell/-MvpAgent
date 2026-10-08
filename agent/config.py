"""集中配置与启动安全检查。"""

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    app_env: str = "development"
    database_path: Path = ROOT / "database" / "ecommerce.db"
    state_db_path: Path = ROOT / "runtime" / "agent_state.db"
    knowledge_path: Path = ROOT / "knowledge" / "source"
    qdrant_url: str = ":memory:"
    mock_server_url: str = "http://127.0.0.1:8081"
    mock_random_seed: int = 20260923
    mock_internal_key: str = "local-demo-internal-key"
    mock_scenario_control_enabled: bool = False
    sql_max_rows: int = 200
    sql_max_corrections: int = 2
    tool_max_attempts: int = 3
    request_deadline_seconds: float = 20.0
    trace_enabled: bool = True
    pii_redaction_enabled: bool = True
    llm_provider: str = "rules"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    metrics_token: str = "demo-metrics-token"
    state_encryption_key: str = ""
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    data_reference_time: str = ""

    def resolved_database_path(self) -> Path:
        return self.database_path if self.database_path.is_absolute() else ROOT / self.database_path

    def resolved_state_path(self) -> Path:
        return self.state_db_path if self.state_db_path.is_absolute() else ROOT / self.state_db_path

    def resolved_knowledge_path(self) -> Path:
        return self.knowledge_path if self.knowledge_path.is_absolute() else ROOT / self.knowledge_path

    def analysis_now(self) -> datetime:
        if self.data_reference_time:
            return datetime.fromisoformat(self.data_reference_time)
        if self.app_env in {"development", "test"}:
            manifest = ROOT / "data_manifest.json"
            if manifest.is_file():
                return datetime.fromisoformat(json.loads(manifest.read_text(encoding="utf-8"))["generated_at"])
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    def fernet(self) -> Fernet:
        if self.state_encryption_key:
            return Fernet(self.state_encryption_key.encode())
        if self.app_env == "production":
            raise ValueError("STATE_ENCRYPTION_KEY is required in production")
        # Only for reproducible synthetic development data. Production requires a managed key.
        material = f"synthetic-demo:{self.mock_random_seed}:{self.mock_internal_key}".encode()
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))

    def validate_runtime(self) -> None:
        if self.app_env == "production":
            if self.mock_internal_key == "local-demo-internal-key":
                raise ValueError("MOCK_INTERNAL_KEY must be set in production")
            if not self.metrics_token or self.metrics_token == "demo-metrics-token":
                raise ValueError("METRICS_TOKEN must be set in production")
            self.fernet()
            raise ValueError("production authentication adapter is not configured")
        if self.sql_max_rows < 1 or self.sql_max_corrections < 0 or self.tool_max_attempts < 1:
            raise ValueError("invalid execution limits")
        if self.request_deadline_seconds <= 0:
            raise ValueError("REQUEST_DEADLINE_SECONDS must be positive")
