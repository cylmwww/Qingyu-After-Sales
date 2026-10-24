"""仅替代外部模型 HTTP 服务，保留生产 provider、解析与图装配。"""

import json
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from pydantic import SecretStr


@pytest.mark.parametrize("array_answer", [False, True], ids=["合法对象", "拒绝数组根"])
async def test_真实模型客户端通过HTTP协议驱动对话(start_app, app_settings, manifest, array_answer):
    replies = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/model_greeting.json").read_text(
            encoding="utf-8"
        )
    )
    remaining = deque([*replies[:-1], [replies[-1]] if array_answer else replies[-1]])
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                }
            )
            if not remaining:
                self.send_error(500)
                return
            content = json.dumps(remaining.popleft(), ensure_ascii=False)
            wire = json.dumps(
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = app_settings.model_copy(
        update={
            "deepseek_api_key": SecretStr("test-only-key"),
            "deepseek_base_url": f"http://127.0.0.1:{server.server_port}",
            "llm_max_retries": 1,
        }
    )
    try:
        async with start_app(settings) as (client, _container):
            response = await client.post(
                "/scenario/chat", json={"message": "你好", "user_id": "u_demo"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["strategy"] == "direct"
            assert body["intent"] == "greeting"
            if array_answer:
                # 数组根不能被静默接受为回答；真实生成节点走已有降级路径。
                assert body["answer"] != replies[-1]["answer"]
                assert body["answer"]
            else:
                assert body["answer"] == replies[-1]["answer"]
                assert body["token_usage"]["total_tokens"] > 0
            assert not remaining
            assert requests
            for request in requests:
                assert request["path"] == "/chat/completions"
                assert request["authorization"] == "Bearer test-only-key"
                assert request["payload"]["model"] == settings.deepseek_model
                assert request["payload"]["response_format"] == {"type": "json_object"}
                assert {message["role"] for message in request["payload"]["messages"]} == {
                    "system",
                    "user",
                }
            manifest(
                "model_protocol",
                {
                    "intent": body["intent"],
                    "strategy": body["strategy"],
                    "answer": body["answer"],
                    "protocol": [
                        {
                            "path": request["path"],
                            "model": request["payload"]["model"],
                            "response_format": request["payload"]["response_format"],
                        }
                        for request in requests
                    ],
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
