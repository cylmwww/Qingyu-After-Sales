"""模型查询列表的清理边界；功能降级由真实 HTTP 场景覆盖。"""

import pytest

from app.providers.fake import FakeLLMProvider
from app.retrieval.query_rewrite import QueryRewriter


@pytest.mark.asyncio
async def test_rewrite_caps_at_three_queries() -> None:
    llm = FakeLLMProvider(canned={"退货": '{"queries": ["a", "b", "c", "d", "e"]}'})
    out = await QueryRewriter(llm).rewrite("退货")
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_rewrite_drops_blank_entries() -> None:
    llm = FakeLLMProvider(canned={"退货": '{"queries": ["   ", "有效查询"]}'})
    out = await QueryRewriter(llm).rewrite("退货")
    assert out == ["有效查询"]
