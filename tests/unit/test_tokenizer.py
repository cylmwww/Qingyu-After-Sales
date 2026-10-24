"""分词输入边界，以及可选 jieba 缺失时的中文双字回退。"""

from __future__ import annotations

from app.retrieval import tokenizer as tokenizer_mod
from app.retrieval.tokenizer import tokenize


def test_tokenize_empty_input() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_tokenize_fallback_splits_han_into_bigrams(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer_mod, "jieba", None)
    assert tokenize("退货政策") == ["退货", "货政", "政策"]


def test_tokenize_fallback_keeps_lone_han_character(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer_mod, "jieba", None)
    assert tokenize("退") == ["退"]


def test_tokenize_fallback_extracts_latin_words(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer_mod, "jieba", None)
    tokens = tokenize("退款 refund 2024")
    assert "refund" in tokens
    assert "2024" in tokens


def test_tokenize_fallback_lowercases_latin(monkeypatch) -> None:
    monkeypatch.setattr(tokenizer_mod, "jieba", None)
    assert "rag" in tokenize("RAG")
