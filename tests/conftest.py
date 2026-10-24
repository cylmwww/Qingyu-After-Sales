"""真实应用测试装配：隔离外部配置与数据库，保留内部生产路径。"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings, clear_settings_cache
from app.main import create_app
from app.services.seed import seed_demo_data

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    # 环境边界也保持隔离，以便配置接线回归时不会触碰开发数据库或服务。
    overrides = {
        "APP_ENV": "development",
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'environment.db').as_posix()}",
        "DEEPSEEK_API_KEY": "",
        "EMBEDDING_API_KEY": "",
        "EMBEDDING_BASE_URL": "",
        "REDIS_URL": "",
        "QDRANT_URL": "",
        "MCP_REMOTE_URL": "",
        "LOCAL_EMBEDDING_ENABLED": "false",
        "LOCAL_EMBEDDING_FORCE": "false",
        "CROSS_ENCODER_ENABLED": "false",
        "CROSS_ENCODER_FORCE": "false",
        "UNSTRUCTURED_ENABLED": "false",
        "UNSTRUCTURED_FORCE": "false",
        "KNOWLEDGE_DIR": str(ROOT / "knowledge"),
        "ADMIN_API_TOKEN": "",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'scenario.db').as_posix()}",
        api_v1_prefix="/scenario",
        admin_api_token=SecretStr("scenario-admin"),
        rate_limit_per_minute=100,
    )
    yield settings
    clear_settings_cache()


@pytest.fixture
def manifest(tmp_path):
    def write(name, result):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    return write


@pytest.fixture
def start_app(app_settings):
    @asynccontextmanager
    async def start(settings=None):
        app = create_app(settings or app_settings)
        async with app.router.lifespan_context(app):
            async with app.state.container.session_factory() as db:
                await seed_demo_data(db)
                await db.commit()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test.local"
            ) as client:
                yield client, app.state.container

    return start
