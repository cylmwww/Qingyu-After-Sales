from pathlib import Path

from app.core.constants import PII_MASK
from app.core.enums import DocumentCategory
from app.retrieval.cleaning import dedupe_chunks, normalize_text
from app.retrieval.unstructured_parser import elements_to_chunks, parse_path
from app.schemas.retrieval import RetrievedChunk
from tests.unit.test_unstructured_parser import _FakeElement, _settings


def _chunk(chunk_id: str, document_id: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        title=document_id,
        content=content,
        score=0.0,
        category=DocumentCategory.FAQ,
    )


def test_joins_cjk_hard_breaks_but_not_tables() -> None:
    assert normalize_text("无理\n由", join_hard_breaks=True) == "无理由"
    assert "\n" in normalize_text("无理\n由", join_hard_breaks=False)
    chunks = elements_to_chunks(
        [_FakeElement("Table", "无理\n由", table_html="<table>无理\n由</table>")],
        document_id="shipping",
        title="shipping",
        category=DocumentCategory.SHIPPING,
        source="t.pdf",
    )
    assert "\n" in chunks[0].content


def test_non_markdown_masks_phone(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-fake")

    def fake_partition(_path: Path) -> list[_FakeElement]:
        return [
            _FakeElement(
                "Table",
                "用户手机 13812345678 已登记。",
                table_html="<table><tr><td>13812345678</td></tr></table>",
            )
        ]

    chunks = parse_path(
        path,
        document_id="scan",
        title="scan",
        category=DocumentCategory.FAQ,
        settings=_settings(),
        partition_fn=fake_partition,
    )
    assert chunks
    assert "13812345678" not in chunks[0].content
    assert f"138{PII_MASK}5678" in chunks[0].content
    assert "13812345678" not in chunks[0].metadata["table_html"]
    assert f"138{PII_MASK}5678" in chunks[0].metadata["table_html"]


def test_similar_policies_with_different_deadlines_are_both_kept() -> None:
    text_a = "非定制未拆封影响二次销售的商品签收次日起7个自然日内可申请无理由退货。"
    text_b = "非定制未拆封影响二次销售的商品签收次日起15个自然日内可申请无理由退货。"
    kept = dedupe_chunks(
        [_chunk("a_00", "return_policy", text_a), _chunk("b_00", "return_electronics", text_b)]
    )
    assert [(chunk.chunk_id, chunk.content) for chunk in kept] == [
        ("a_00", text_a),
        ("b_00", text_b),
    ]
