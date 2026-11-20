from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup, Tag
from inkpress.ai.client import AIClient
from inkpress.ai.editorial import (
    _clean_llm_output,
    _parse_numbered_translations,
    generate_frontpage_briefing,
    summarize_article,
    translate_article_bilingual,
)
from inkpress.db.models import Article
from openai import BadRequestError


def _make_article(
    *,
    title: str = "Systems Engineering at Scale",
    clean_html: str = "",
    plain_text: str = "",
    language: str = "en",
    category: str = "科技",
    source_name: str = "Test Blog",
) -> Article:
    return Article(
        id=1,
        canonical_url="https://example.com/post",
        content_hash="abc123",
        title=title,
        source_name=source_name,
        category=category,
        language=language,
        clean_html=clean_html,
        plain_text=plain_text,
    )


class _StubAI(AIClient):
    """Lightweight test double for AIClient.chat."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return self._responses.pop(0) if self._responses else ""


def test_clean_llm_output_strips_think_and_code_fences() -> None:
    raw = (
        "<think>\nLet me analyze the 20 articles carefully...\nFirst point.\n</think>\n\n"
        "```markdown\n"
        "**编者按**：今日科技与财经并进。\n\n"
        "- **要点一**：AI 推理成本下降。\n"
        "```"
    )
    cleaned = _clean_llm_output(raw)
    assert "<think>" not in cleaned
    assert "Let me analyze" not in cleaned
    assert "```" not in cleaned
    assert "**编者按**：今日科技与财经并进。" in cleaned

    # Unclosed <think> (truncated reasoning) must never leak
    assert _clean_llm_output("<think>Still thinking about stuff...") == ""
    # Lone </think> closing tag strips preceding trace
    assert _clean_llm_output("internal thought\n</think>\nFinal answer") == "Final answer"


def test_parse_numbered_translations_sequential_multiline_no_overwrite() -> None:
    raw = (
        "<think>translating</think>\n"
        "[0] 规模化系统工程\n"
        "[1] 第一段译文前半句，\n"
        "第一段译文后半句。\n"
        "[2] 第二段译文。\n"
        "[3] 第三段包含如下引用：\n"
        "[1] 文献一：分布式系统导论。\n"
        "[4] 第四段收尾。"
    )
    parsed = _parse_numbered_translations(raw, [0, 1, 2, 3, 4])
    assert parsed[0] == "规模化系统工程"
    assert parsed[1] == "第一段译文前半句，\n第一段译文后半句。"
    assert parsed[2] == "第二段译文。"
    assert "[1] 文献一：分布式系统导论。" in parsed[3]
    assert parsed[4] == "第四段收尾。"


async def test_ai_client_reasoning_fallback_and_length_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AIClient(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="o3-mini",
    )
    recorded_kwargs: list[dict[str, Any]] = []
    warnings: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        "inkpress.ai.client.logger.warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    async def fake_create(**kwargs: Any) -> Any:
        recorded_kwargs.append(kwargs)
        if "max_tokens" in kwargs:
            req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
            resp = httpx.Response(400, request=req)
            raise BadRequestError(
                "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead.",
                response=resp,
                body=None,
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content="OK"),
                )
            ]
        )

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    try:
        res = await client.test_connection()
        assert res["ok"] is True
        assert res["reply"] == "OK"
        assert len(recorded_kwargs) == 2
        assert recorded_kwargs[0]["max_tokens"] == 128
        assert "max_completion_tokens" in recorded_kwargs[1]
        assert recorded_kwargs[1]["max_completion_tokens"] == 128
        assert "temperature" not in recorded_kwargs[1]
        assert any(ev == "ai_response_truncated" for ev, _ in warnings)

        # Also test temperature-only rejection fallback
        recorded_kwargs.clear()

        async def fake_create_temp_reject(**kwargs: Any) -> Any:
            recorded_kwargs.append(kwargs)
            if "temperature" in kwargs:
                req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
                resp = httpx.Response(400, request=req)
                raise BadRequestError(
                    "Unsupported value: 'temperature' does not support 0.2 with this model.",
                    response=resp,
                    body=None,
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="Done"),
                    )
                ]
            )

        monkeypatch.setattr(client._client.chat.completions, "create", fake_create_temp_reject)
        out = await client.chat(system="sys", user="hi", temperature=0.2, max_tokens=500)
        assert out == "Done"
        assert len(recorded_kwargs) == 2
        assert "temperature" not in recorded_kwargs[1]
        assert recorded_kwargs[1]["max_completion_tokens"] == 500
    finally:
        await client.close()


async def test_summarize_and_frontpage_briefing_clean_output_and_tokens() -> None:
    art = _make_article(
        title="Next-Gen E-Ink",
        plain_text="Electronic ink displays provide crisp reading comfort. " * 20,
    )
    ai_sum = _StubAI(["<think>draft</think>\n这是一篇关于新一代电子纸显示技术的深度导读。"])
    summary = await summarize_article(ai_sum, art)
    assert summary == "这是一篇关于新一代电子纸显示技术的深度导读。"
    assert ai_sum.calls[0]["max_tokens"] == 1000

    ai_fp = _StubAI(
        [
            "<think>plan</think>\n```markdown\n## 【今日头版综述】\n\n"
            "今日科技聚焦电子纸与自托管阅读生态。\n\n"
            "**核心看点**：开源出版工具走向成熟。\n```"
        ]
    )
    briefing_html = await generate_frontpage_briefing(ai_fp, "每日晨报", [art])
    assert ai_fp.calls[0]["max_tokens"] == 3200
    assert "今日头版综述" not in briefing_html
    assert "<think>" not in briefing_html
    assert "plan" not in briefing_html
    assert "电子纸与自托管阅读生态" in briefing_html
    assert "<strong>核心看点</strong>" in briefing_html


async def test_bilingual_skips_chinese_articles() -> None:
    zh_art = _make_article(
        title="中文深度长文",
        clean_html="<p>这是一篇原本就是中文撰写的深度分析文章，不需要翻译。</p>",
        plain_text="这是一篇原本就是中文撰写的深度分析文章，不需要翻译。",
        language="zh-CN",
    )
    ai = _StubAI(["should not be called"])
    tr_title, bi_html = await translate_article_bilingual(ai, zh_art)
    assert tr_title is None
    assert bi_html is None
    assert ai.calls == []


async def test_bilingual_leaf_blocks_li_headings_and_chunked_batching() -> None:
    # Build 28 leaf blocks (exceeds old 24-block cap!):
    # 1 short h2, 1 nested blockquote>p, 2 li items, and 24 paragraphs -> 28 blocks => 3 batches (12 + 12 + 4)
    parts = [
        "<h2>Overview</h2>",
        "<blockquote><p>A nested quote inside paragraph should be translated once.</p></blockquote>",
        "<ul><li>Fast startup time</li><li>Low memory footprint</li></ul>",
    ]
    for i in range(1, 25):
        parts.append(f"<p>Paragraph number {i} discusses reliable engineering practices in detail.</p>")

    en_art = _make_article(
        title="Engineering Deep Dive",
        clean_html="\n".join(parts),
        plain_text="Engineering Deep Dive",
        language="en",
    )

    batch1_resp = "\n".join(
        ["[0] 工程深度解析"] + [f"[{i}] 第一批译文 {i}" for i in range(1, 13)]
    )
    batch2_resp = "\n".join(f"[{i}] 第二批译文 {i}" for i in range(1, 13))
    batch3_resp = "\n".join(f"[{i}] 第三批译文 {i}" for i in range(1, 5))

    ai = _StubAI([batch1_resp, batch2_resp, batch3_resp])
    tr_title, bi_html = await translate_article_bilingual(ai, en_art)

    assert tr_title == "工程深度解析"
    assert bi_html is not None
    assert len(ai.calls) == 3
    assert all(call["max_tokens"] == 4096 for call in ai.calls)

    soup = BeautifulSoup(bi_html, "lxml")
    tr_tags = soup.find_all("p", class_="bilingual-tr")
    # Exactly 28 translated leaf blocks (no blockquote duplication, no 24-block truncation!)
    assert len(tr_tags) == 28
    # Blockquote must have only 1 translation inside it, not 2
    bq = soup.find("blockquote")
    assert isinstance(bq, Tag)
    assert len(bq.find_all("p", class_="bilingual-tr")) == 1
    # <li> translation must live inside <li> (valid XHTML, not sibling inside <ul>)
    ul = soup.find("ul")
    assert isinstance(ul, Tag)
    assert ul.find("p", recursive=False) is None
    li_tags = [el for el in ul.find_all("li") if isinstance(el, Tag)]
    assert len(li_tags) == 2
    assert all(li.find("p", class_="bilingual-tr") is not None for li in li_tags)
    # 28th block (paragraph 24) got translated in batch 3
    assert "第三批译文 4" in bi_html


async def test_bilingual_rejects_low_coverage_to_prevent_cache_poisoning() -> None:
    html = "\n".join(
        f"<p>Sentence {i} about distributed database indexing and storage engines.</p>"
        for i in range(1, 11)
    )
    art = _make_article(title="Database Internals", clean_html=html, language="en")

    # Only 2 of 10 paragraphs parsed (20% < 30%) -> must return (None, None)
    ai = _StubAI(["[0] 数据库内幕\n[1] 译文一\n[2] 译文二"])
    tr_title, bi_html = await translate_article_bilingual(ai, art)
    assert tr_title is None
    assert bi_html is None

    # Completely malformed unnumbered output (0%) -> must return (None, None)
    ai_bad = _StubAI(["Sorry, I cannot follow the numbering format."])
    tr_title2, bi_html2 = await translate_article_bilingual(ai_bad, art)
    assert tr_title2 is None
    assert bi_html2 is None
