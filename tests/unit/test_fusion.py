from copy import deepcopy

import pytest

from app.retrieval.fusion import reciprocal_rank_fusion
from app.schemas.retrieval import RetrievedChunk


def 创建片段(chunk_id: str, score: float = 1.0, **fields) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"document-{chunk_id}",
        title=f"规则-{chunk_id}",
        content=f"规则内容-{chunk_id}",
        score=score,
        **fields,
    )


@pytest.mark.parametrize("ranked_lists", [[], [[]], [[], []]], ids=["无检索路", "单空路", "多空路"])
def test_空检索列表没有融合结果(ranked_lists):
    assert reciprocal_rank_fusion(ranked_lists) == []


@pytest.mark.parametrize("k", [0, -1, -60], ids=["零", "负一", "负六十"])
def test_非正融合常数被拒绝(k):
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[创建片段("a")]], k=k)


def test_跨路重复结果累加名次权重并提升排名():
    ranked_lists = [
        [创建片段("a", 1000), 创建片段("b", 0.01)],
        [],
        [创建片段("c", 0.9), 创建片段("b", 0.8)],
    ]

    result = reciprocal_rank_fusion(ranked_lists, k=1)

    assert result[0].chunk_id == "b"
    assert {chunk.chunk_id for chunk in result} == {"a", "b", "c"}
    assert len(result) == 3
    # b 两次第二名：1/3 + 1/3；a、c 各一次第一名：1/2。
    assert result[0].score == pytest.approx(2 / 3)
    assert [chunk.score for chunk in result[1:]] == pytest.approx([1 / 2, 1 / 2])
    assert [chunk.rank for chunk in result] == [1, 2, 3]
    assert all(chunk.source == "rrf" for chunk in result)


def test_重复片段保留首份完整元数据且输入不变():
    first = 创建片段(
        "a",
        99,
        rank=8,
        source="bm25",
        section="售后",
        chunk_index=2,
        policy_version="v2",
        metadata={"地区": "华东"},
    )
    later = first.model_copy(
        update={
            "document_id": "另一文档",
            "title": "后出现标题",
            "content": "后出现内容",
            "section": "其他章节",
            "policy_version": "v3",
            "metadata": {"地区": "华南"},
            "score": 0.5,
            "rank": 1,
            "source": "dense",
        }
    )
    ranked_lists = [[first], [later]]
    before = deepcopy(ranked_lists)

    result = reciprocal_rank_fusion(ranked_lists, k=1)

    assert len(result) == 1
    assert result[0].model_dump(exclude={"score", "rank", "source"}) == first.model_dump(
        exclude={"score", "rank", "source"},
    )
    assert result[0].score == pytest.approx(1.0)
    assert ranked_lists == before
    # 调用方修改融合结果也不能回写原始检索结果。
    result[0].metadata["地区"] = "华北"
    result[0].title = "调用方修改标题"
    assert ranked_lists == before


def test_原始分数尺度不影响融合结果():
    ranked_lists = [
        [创建片段("a", 100), 创建片段("b", 10)],
        [创建片段("b", 0.9), 创建片段("c", 0.1)],
    ]
    rescaled = [
        [
            ranked_lists[0][0].model_copy(update={"score": 1e12}),
            ranked_lists[0][1].model_copy(update={"score": 1e6}),
        ],
        [
            ranked_lists[1][0].model_copy(update={"score": 1e-6}),
            ranked_lists[1][1].model_copy(update={"score": 1e-12}),
        ],
    ]

    assert reciprocal_rank_fusion(rescaled) == reciprocal_rank_fusion(ranked_lists)
