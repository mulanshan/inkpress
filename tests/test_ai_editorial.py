from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup, Tag
from inkpress.ai.client import AIClient
from inkpress.ai.editorial import (
    DigestCandidate,
    _clean_llm_output,
    _parse_numbered_translations,
    cluster_and_filter_candidates,
    curate_illustrated_digest,
    extract_candidates_from_articles,
    extract_tugua_topics,
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


async def test_curate_illustrated_digest_deduplicates_merges_and_embeds_images() -> None:
    tugua_text = (
        "每天一图卦，让我们更清楚地了解这个世界\n"
        "【1】商务部：坚决反对美对中国等经济体加征301关税\n"
        "【2】神舟二十四号巴基斯坦航天员准备进驻天宫空间站\n"
    )
    excluded = extract_tugua_topics(tugua_text, "【喷嚏图卦20261126】关税与航天")
    assert len(excluded) == 3

    zhihu_digest = Article(
        id=10,
        canonical_url="https://inkpress.local/newsnow/zhihu/2026-11-27",
        content_hash="h10",
        title="知乎热榜 · 热点速览 (2026-11-27)",
        source_name="知乎热榜",
        category="综合",
        extra_meta={
            "digest_items": [
                {
                    "title": "为什么神舟二十四号外籍航天员名额给了巴基斯坦？",
                    "summary": "巴基斯坦航天员随神舟二十四号进驻天宫引发关注。",
                    "info_tag": "850万热度",
                    "url": "https://www.zhihu.com/question/1",
                    "image_url": "https://picx.zhimg.com/space.jpg",
                },
                {
                    "title": "魏建军直言很多车企靠国家补贴生存，拿到的补贴比盈利还多，如何评价？",
                    "summary": "长城汽车董事长魏建军表示部分车企过度依赖地方补贴维持运转。",
                    "info_tag": "320万热度",
                    "url": "https://www.zhihu.com/question/2",
                    "image_url": "",
                },
                {
                    "title": "如何看待今天某明星机场穿搭？",
                    "summary": "",
                    "info_tag": "100万热度",
                    "url": "https://www.zhihu.com/question/3",
                    "image_url": "",
                },
            ]
        },
    )
    ithome_art = Article(
        id=11,
        canonical_url="https://www.ithome.com/0/900/111.htm",
        content_hash="h11",
        title="长城汽车魏建军到访格力：痛斥部分车企靠补贴生存与恶性挖人",
        source_name="IT之家",
        category="科技",
        lead_image_url="https://img.ithome.com/auto_wei.jpg",
        clean_html='<p>魏建军与董明珠会谈时指出汽车行业补贴乱象。</p><img src="https://img.ithome.com/auto_wei.jpg"/>',
        plain_text="魏建军与董明珠会谈时指出汽车行业补贴乱象，呼吁回归健康盈利。",
    )
    wscn_art = Article(
        id=12,
        canonical_url="https://wallstreetcn.com/articles/222",
        content_hash="h12",
        title="央行开展5000亿元MLF操作，中标利率维持2.0%不变",
        source_name="华尔街见闻",
        category="财经",
        lead_image_url="https://wpimg-wscn.awtmt.com/mlf_chart.png",
        clean_html='<p>央行今日开展中期借贷便利操作释放中长期流动性。</p><img src="https://wpimg-wscn.awtmt.com/mlf_chart.png"/>',
        plain_text="央行今日开展中期借贷便利操作释放中长期流动性，保持银行体系充裕。",
    )

    cands = extract_candidates_from_articles([zhihu_digest, ithome_art, wscn_art])
    assert len(cands) == 5

    llm_reply = (
        "<think>Deduplicating space story, merging Wei Jianjun...</think>\n"
        "===ITEM===\n"
        "TITLE: 魏建军直言部分车企靠补贴续命，与格力共议行业乱象\n"
        "SOURCES: 知乎热榜 · IT之家\n"
        "IMAGES: IMG_01\n"
        "BODY:\n"
        "长城汽车董事长魏建军到访格力交流时表示，部分地方过度保护亏损车企，补贴金额甚至超过企业盈利。\n"
        "===END===\n"
        "===ITEM===\n"
        "TITLE: 央行开展5000亿元MLF操作，维持流动性充裕\n"
        "SOURCES: 华尔街见闻\n"
        "IMAGES: IMG_02, IMG_99\n"
        "BODY:\n"
        "央行今日开展5000亿元中期借贷便利操作，利率维持2.0%不变。\n"
        "===END==="
    )
    ai = _StubAI([llm_reply])
    res = await curate_illustrated_digest(ai, cands, excluded_topics=excluded, max_items=10)
    assert res.used_ai is True
    assert res.item_count == 2
    # Space topic from 喷嚏图卦 and bare-title celebrity question were excluded before prompt!
    sent_prompt = ai.calls[0]["user"]
    cand_section = sent_prompt.split("候选资讯：", 1)[1]
    assert "为什么神舟二十四号" not in cand_section
    assert "明星机场穿搭" not in cand_section
    # Merged cluster combined 知乎热榜 + IT之家 and pooled IT之家's image!
    assert "知乎热榜 · IT之家" in sent_prompt
    assert "【1】魏建军直言部分车企靠补贴续命" in res.body_html
    assert "https://img.ithome.com/auto_wei.jpg" in res.body_html
    assert "https://wpimg-wscn.awtmt.com/mlf_chart.png" in res.body_html
    assert "IMG_99" not in res.body_html
    assert res.image_referers.get("https://wpimg-wscn.awtmt.com/mlf_chart.png") == "https://wallstreetcn.com/articles/222"

    # Also verify deterministic non-AI fallback
    fallback = await curate_illustrated_digest(None, cands, excluded_topics=excluded, max_items=10)
    assert fallback.used_ai is False
    assert fallback.item_count == 2
    assert "【1】" in fallback.body_html
    assert 'class="tugua-figure"' in res.body_html


async def test_curate_digest_blocks_mismatched_cross_item_images_and_balances_hotlists() -> None:
    # 1. Verify P0-1: off-by-one / cross-item image mismatch is dropped both with and without REFS
    cands = [
        DigestCandidate(
            title="广州车展继续堆叠800伏、智驾与超长续航",
            summary="广州车展新能源车型密集发布，多家车企主打高压快充。",
            source_name="财联社热门",
            category="财经",
            canonical_url="https://cls.cn/auto",
            images=[],
        ),
        DigestCandidate(
            title="40歲離職去日本讀書：人生下半場的重新選擇",
            summary="一位台灣職場人辭職赴日留學的親身經歷與財務規劃。",
            source_name="今日最新－天下雜誌",
            category="文化",
            canonical_url="https://www.cw.com.tw/article/5140001",
            images=["https://storage.googleapis.com/cw-com-tw/japan_study.jpg"],
        ),
        DigestCandidate(
            title="苹果发布新一代 M5 Ultra 工作站芯片",
            summary="苹果正式推出面向本地大模型推理的高带宽统一内存芯片。",
            source_name="IT之家",
            category="科技",
            canonical_url="https://www.ithome.com/m5",
            images=["https://img.ithome.com/m5_chip.jpg"],
        ),
        DigestCandidate(
            title="比亚迪海豹新款车型续航突破一千公里",
            summary="新款混动技术降低亏电油耗。",
            source_name="IT之家",
            category="科技",
            canonical_url="https://www.ithome.com/byd",
            images=["https://img.ithome.com/byd_car.jpg"],
        ),
    ]

    # LLM shifts image IDs: attaches C02's Japan photo (IMG_01) to C01 Guangzhou Auto Show,
    # and attaches C04's BYD car photo (IMG_03) to C03 Apple M5 chip (same source IT之家!)
    drifted_llm = (
        "===ITEM===\n"
        "TITLE: 广州车展继续堆叠800伏、智驾与超长续航\n"
        "SOURCES: 财联社热门\n"
        "REFS: C01\n"
        "IMAGES: IMG_01\n"
        "BODY:\n"
        "广州车展新能源车型密集发布，重点聚焦800伏高压平台与高阶智驾。\n"
        "===END===\n"
        "===ITEM===\n"
        "TITLE: 苹果发布新一代 M5 Ultra 工作站芯片\n"
        "SOURCES: IT之家\n"
        "IMAGES: IMG_03\n"
        "BODY:\n"
        "苹果推出面向大模型工作站的高带宽统一内存处理器。\n"
        "===END===\n"
        "===ITEM===\n"
        "TITLE: 40歲離職去日本讀書的財務規劃\n"
        "SOURCES: 今日最新－天下雜誌\n"
        "REFS: C02\n"
        "IMAGES: IMG_01\n"
        "BODY:\n"
        "台灣職場人分享40歲赴日留學的準備與心路歷程。\n"
        "===END==="
    )
    ai = _StubAI([drifted_llm])
    res = await curate_illustrated_digest(ai, cands, max_items=10)
    assert res.used_ai is True
    assert res.item_count == 3
    soup = BeautifulSoup(res.body_html, "lxml")
    tugua_divs = soup.find_all("div", class_="tugua-item")
    assert len(tugua_divs) == 3
    # Item 1 (Guangzhou Auto Show) MUST NOT contain Japan study image!
    assert "japan_study.jpg" not in str(tugua_divs[0])
    # Item 2 (Apple M5 chip from IT之家) MUST NOT contain BYD car image from IT之家!
    assert "byd_car.jpg" not in str(tugua_divs[1])
    # Item 3 (Japan study) legitimately owns IMG_01 and embeds it!
    assert "japan_study.jpg" in str(tugua_divs[2])

    # 2. Verify P1-2: 10-20 char Chinese declarative hotlist & English HN headlines survive,
    #    while gossip/questions are dropped and per-digest bundle is capped at 6
    big_weibo = Article(
        id=99,
        canonical_url="https://inkpress.local/newsnow/weibo/2026-11-27",
        content_hash="wb99",
        title="微博热搜",
        source_name="微博热搜",
        category="热点",
        extra_meta={
            "digest_items": [
                {"title": f"商务部回应第{i}轮经贸磋商进展", "summary": "", "url": f"https://s.weibo.com/{i}"}
                for i in range(1, 15)
            ]
        },
    )
    extracted = extract_candidates_from_articles([big_weibo])
    assert len(extracted) == 6  # Capped per digest source for balance!

    filtered = cluster_and_filter_candidates(
        [
            DigestCandidate("商务部回应经贸磋商最新进展", "", "微博热搜", "热点"),  # 13 chars CJK declarative -> keep
            DigestCandidate("SQLite 3.50 introduces native SIMD page compression", "", "Hacker News", "科技"),  # English declarative -> keep
            DigestCandidate("某明星红毯生图曝光", "", "微博热搜", "热点"),  # Gossip -> drop
            DigestCandidate("如何评价今年秋招行情？", "", "知乎热榜", "综合"),  # Bare question -> drop
        ]
    )
    kept_titles = {c.title for c in filtered}
    assert "商务部回应经贸磋商最新进展" in kept_titles
    assert "SQLite 3.50 introduces native SIMD page compression" in kept_titles
    assert "某明星红毯生图曝光" not in kept_titles
    assert "如何评价今年秋招行情？" not in kept_titles
