"""从真实 HTTP 入口核对应用配置与基础契约。"""


async def test_显式配置同时约束路由和启动容器(start_app, tmp_path, manifest):
    async with start_app() as (client, _container):
        assert (await client.get("/scenario/health")).status_code == 200
        assert (await client.get("/scenario/admin/tools")).status_code == 401
        response = await client.get(
            "/scenario/admin/tools", headers={"x-admin-token": "scenario-admin"}
        )
        assert response.status_code == 200
        assert "cancel_order" in response.json()["tools"]
        assert (tmp_path / "scenario.db").is_file()
        assert not (tmp_path / "environment.db").exists()
        ready = await client.get("/scenario/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert any(
            dep["name"] == "database" and dep["ready"] for dep in ready.json()["dependencies"]
        )
        capabilities = (await client.get("/scenario/capabilities")).json()
        assert capabilities["database"] == "sqlite"
        assert capabilities["llm"] == "offline"
        assert capabilities["cache"] == capabilities["vector_store"] == "memory"
        manifest(
            "runtime",
            {"capabilities": capabilities, "admin_without_token": 401, "isolated_database": True},
        )


async def test_无效请求错误与追踪关联(start_app, manifest):
    async with start_app() as (client, _container):
        statuses = []
        for message in ("", "问" * 4001):
            response = await client.post(
                "/scenario/chat",
                json={"message": message},
                headers={"x-trace-id": "invalid-trace", "x-request-id": "invalid-request"},
            )
            assert response.status_code == 422
            body = response.json()
            assert body["error_code"] == "INVALID_REQUEST"
            assert body["trace_id"] == response.headers["x-trace-id"] == "invalid-trace"
            assert response.headers["x-request-id"] == "invalid-request"
            statuses.append(response.status_code)
        manifest("validation", {"statuses": statuses, "error_code": "INVALID_REQUEST"})
