"""在外部模型 HTTP 边界制造失败，保留真实图、知识检索与消息持久化。"""

import asyncio
import json
import re
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.providers.embeddings import build_embedding_provider
from app.repositories.models import MessageRow
from app.retrieval.ingest import KnowledgeIngestor

pytestmark = pytest.mark.asyncio(loop_scope="session")

QUESTION = "偏远地区包邮规则是什么？"
SUPPORTED = {"supported": True, "hallucinated_spans": [], "rationale": "有证据"}


@dataclass
class ModelReply:
    stage: str
    content: object = None
    status: int = 200
    raw_json: str | None = None


@contextmanager
def model_http(app_settings, replies, **overrides):
    """顺序脚本仅替代模型服务；支持原始 JSON 文本和 HTTP 错误。"""
    pending = deque(replies)
    received = []
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not pending:
                failures.append("模型脚本已耗尽，仍收到请求")
                self.send_error(401)
                return
            reply = pending.popleft()
            received.append({"stage": reply.stage, "payload": payload})
            try:
                content = reply.content(payload) if callable(reply.content) else reply.content
                text = reply.raw_json
                if text is None:
                    text = json.dumps(content, ensure_ascii=False)
                body = {"choices": [{"message": {"content": text}}]}
                wire = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except Exception as exc:
                failures.append(str(exc))
                self.send_error(401)
                return
            self.send_response(reply.status)
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
            **overrides,
        }
    )
    try:
        yield settings, received
        assert not failures, failures
        assert not pending, [reply.stage for reply in pending]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def routing(strategy):
    return [
        ModelReply(
            "intent",
            {"label": "faq_policy", "confidence": 0.99, "prompt_version": "scenario"},
        ),
        ModelReply(
            "route",
            {
                "complexity": "multi_hop" if strategy == "multi" else "single_hop",
                "strategy": strategy,
                "confidence": 0.99,
            },
        ),
    ]


def evidence_answer(payload):
    # 只读取收到的片段边界，不匹配或锁定提示词文案。
    text = payload["messages"][-1]["content"]
    chunks = re.findall(r"^\[([^\]\n]+)\] [^\n]*\n([^\n]+)", text, re.MULTILINE)
    assert chunks, "生产检索没有向模型提供证据"
    chunk_id, excerpt = chunks[0]
    return {"answer": excerpt, "citation_chunk_ids": [chunk_id], "refused": False}


async def verify_knowledge(body, settings):
    assert not body["refused"]
    assert body["citations"]
    chunks, _ = await KnowledgeIngestor(
        settings.knowledge_dir, build_embedding_provider(settings), settings
    ).load()
    corpus = {chunk.chunk_id: chunk for chunk in chunks}
    ids = [citation["chunk_id"] for citation in body["citations"]]
    assert len(ids) == len(set(ids))
    for citation in body["citations"]:
        chunk = corpus[citation["chunk_id"]]
        assert citation["document_id"] == chunk.document_id
        assert citation["title"] == chunk.title
    assert body["answer"] in corpus[ids[0]].content
    return ids


@pytest.mark.parametrize("grading", ["充分", "持续不足", "评分异常"])
async def test_多跳检索有界且评分异常不丢证据(start_app, app_settings, manifest, grading):
    grade = {"relevant": True, "sufficient": grading == "充分", "missing_aspect": "包邮条件"}
    grade_reply = (
        ModelReply("grade", status=401) if grading == "评分异常" else ModelReply("grade", grade)
    )
    hops = 2 if grading == "持续不足" else 1
    replies = routing("multi")
    for _ in range(hops):
        replies.extend([ModelReply("rewrite", {"queries": [QUESTION]}), grade_reply])
    replies.extend([ModelReply("answer", evidence_answer), ModelReply("faithfulness", SUPPORTED)])
    with model_http(app_settings, replies, max_retrieval_hops=2) as (settings, received):
        async with start_app(settings) as (client, _container):
            response = await asyncio.wait_for(
                client.post("/scenario/chat", json={"message": QUESTION, "user_id": "u_demo"}),
                timeout=20,
            )
            assert response.status_code == 200
            body = response.json()
            assert body["strategy"] == "multi"
            ids = await verify_knowledge(body, settings)
            manifest(
                "multi_" + grading,
                {
                    "strategy": body["strategy"],
                    "grading": grading,
                    "citations": ids,
                    "answer": body["answer"],
                    "model_stages": [request["stage"] for request in received],
                },
            )


@pytest.mark.parametrize("failure", ["空列表", "缺字段", "坏JSON", "HTTP401"])
async def test_改写失败仍以原问题真实检索(start_app, app_settings, manifest, failure):
    rewrite = {
        "空列表": ModelReply("rewrite", {"queries": []}),
        "缺字段": ModelReply("rewrite", {}),
        "坏JSON": ModelReply("rewrite", raw_json='{"queries":'),
        "HTTP401": ModelReply("rewrite", status=401),
    }[failure]
    replies = [
        *routing("single"),
        rewrite,
        ModelReply("answer", evidence_answer),
        ModelReply("faithfulness", SUPPORTED),
    ]
    with model_http(app_settings, replies) as (settings, received):
        async with start_app(settings) as (client, _container):
            response = await client.post(
                "/scenario/chat", json={"message": QUESTION, "user_id": "u_demo"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["strategy"] == "single"
            ids = await verify_knowledge(body, settings)
            manifest(
                "rewrite_" + failure,
                {
                    "failure": failure,
                    "citations": ids,
                    "answer": body["answer"],
                    "model_stages": [request["stage"] for request in received],
                },
            )


@pytest.mark.parametrize(
    "query,tool_name,arguments,expected",
    [
        ("查询订单 ORD99999", "get_order", {"order_id": "ORD99999"}, "search_knowledge"),
        ("门店营业安排", "mcp_store_hours", {}, "mcp_store_hours"),
        ("仓库截单时间", "mcp_warehouse_cutoff", {}, "mcp_warehouse_cutoff"),
    ],
)
async def test_生产工具执行结果进入最终生成(
    start_app, app_settings, manifest, query, tool_name, arguments, expected
):
    observed = []
    expected_answers = []

    def answer_from_tools(payload):
        text = payload["messages"][-1]["content"]
        # 定位序列化工具结果，以字段验证数据，不绑定周围提示词文案。
        start = text.index('[{"name":')
        results, _ = json.JSONDecoder().raw_decode(text[start:])
        result = results[0]
        assert result["success"] is True
        assert result["name"] == expected
        output = result["output"]
        if expected == "search_knowledge":
            assert output["fallback_from"] == "get_order"
            assert output["chunks"]
            answer = output["chunks"][0]["content"]
        else:
            assert output["source"] == "mcp_local"
            answer = output["weekday"] if expected == "mcp_store_hours" else output["cutoff"]
        observed.append(result)
        expected_answers.append(answer)
        return {"answer": answer, "citation_chunk_ids": [], "refused": False}

    replies = [
        ModelReply(
            "intent",
            {
                "label": "order_inquiry" if arguments else "faq_policy",
                "confidence": 0.99,
                "prompt_version": "scenario",
            },
        ),
        ModelReply(
            "route", {"complexity": "tool_required", "strategy": "agent", "confidence": 0.99}
        ),
        ModelReply(
            "plan",
            {
                "steps": [
                    {
                        "step_id": 1,
                        "goal": "业务查询",
                        "tool_name": tool_name,
                        "arguments": arguments,
                    }
                ]
            },
        ),
        ModelReply("answer", answer_from_tools),
        ModelReply("faithfulness", SUPPORTED),
    ]
    with model_http(app_settings, replies, mcp_enabled=True) as (settings, _received):
        async with start_app(settings) as (client, _container):
            response = await client.post(
                "/scenario/chat", json={"message": query, "user_id": "u_demo"}
            )
            assert response.status_code == 200
            body = response.json()
            assert observed
            assert body["answer"] == expected_answers[0]
            assert body["pending_action"] is None
            assert not body["refused"]
            manifest(
                "production_tool",
                {"tool": expected, "answer": body["answer"], "success": observed[0]["success"]},
            )


async def test_不忠实候选纠正后只发布和保存最终回答(start_app, app_settings, manifest):
    candidate = "不受证据支持的候选：所有偏远地区都保证免费当日送达。"
    replies = [
        *routing("single"),
        ModelReply("rewrite", {"queries": [QUESTION]}),
        ModelReply(
            "candidate",
            lambda payload: {**evidence_answer(payload), "answer": candidate},
        ),
        ModelReply(
            "reject",
            {"supported": False, "hallucinated_spans": [candidate], "rationale": "证据无此承诺"},
        ),
        ModelReply("correct", evidence_answer),
        ModelReply("accept", SUPPORTED),
    ]
    with model_http(app_settings, replies) as (settings, received):
        async with start_app(settings) as (client, container):
            response = await client.post(
                "/scenario/chat/stream", json={"message": QUESTION, "user_id": "u_demo"}
            )
            assert response.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            assert events[0]["event"] == "started"
            assert events[-1]["event"] == "done"
            answers = [event["data"] for event in events if event["event"] == "answer"]
            assert len(answers) == 1
            body = answers[0]
            ids = await verify_knowledge(body, settings)
            assert candidate not in response.text
            assert (
                "".join(event["text"] for event in events if event["event"] == "token")
                == body["answer"]
            )
            async with container.session_factory() as db:
                messages = (
                    await db.scalars(
                        select(MessageRow)
                        .where(MessageRow.session_id == body["session_id"])
                        .order_by(MessageRow.id)
                    )
                ).all()
            assert [(row.role, row.content) for row in messages] == [
                ("user", QUESTION),
                ("assistant", body["answer"]),
            ]
            manifest(
                "corrected_stream",
                {
                    "answer": body["answer"],
                    "citations": ids,
                    "events": [event["event"] for event in events],
                    "persisted_roles": [row.role for row in messages],
                    "candidate_published": False,
                    "model_stages": [request["stage"] for request in received],
                },
            )
