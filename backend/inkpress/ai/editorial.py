from __future__ import annotations

import asyncio
import re

import markdown as md_lib
from bs4 import BeautifulSoup, Tag

from inkpress.ai.client import AIClient
from inkpress.db.models import Article
from inkpress.ingestion.extractor import sanitize_article_html
from inkpress.logging import get_logger

logger = get_logger(__name__)

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_OUTER_CODE_FENCE_RE = re.compile(
    r"^```(?:markdown|md|text|txt|html)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*$",
    re.IGNORECASE | re.DOTALL,
)
_OPENING_CODE_FENCE_RE = re.compile(
    r"^```(?:markdown|md|text|txt|html)?[ \t]*\r?\n",
    re.IGNORECASE,
)
_FRONTPAGE_ECHO_RE = re.compile(
    r"^\s*(?:#{1,3}\s*)?(?:\*{1,2})?\s*【?\s*今日头版综述\s*】?\s*(?:\*{1,2})?\s*[:：]?\s*(?:\r?\n|$)"
)
_MARKER_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*{1,2})?\[(\d+)\](?:\*{1,2})?\s*(.*)$")

_TRANSLATABLE_BLOCK_TAGS = ("p", "h2", "h3", "blockquote", "li")
_BILINGUAL_BATCH_SIZE = 12
_BILINGUAL_MIN_COVERAGE = 0.3


def _clean_llm_output(raw: str) -> str:
    """Strip reasoning `<think>...</think>` blocks and outer Markdown code fences."""
    if not raw:
        return ""
    text = _THINK_BLOCK_RE.sub("", raw)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    text = _UNCLOSED_THINK_RE.sub("", text).strip()

    fence_match = _OUTER_CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()
    elif _OPENING_CODE_FENCE_RE.match(text) and "```" not in text[3:]:
        text = _OPENING_CODE_FENCE_RE.sub("", text, count=1).strip()

    return text


def _is_translatable_block(tag: Tag) -> bool:
    classes = tag.get("class")
    if isinstance(classes, list) and "bilingual-tr" in classes:
        return False
    if isinstance(classes, str) and "bilingual-tr" in classes.split():
        return False

    text = tag.get_text(" ", strip=True)
    if not text:
        return False

    if tag.name in ("h2", "h3"):
        min_len = 2
    elif tag.name == "li":
        min_len = 4
    else:
        min_len = 12

    if len(text) < min_len:
        return False

    # Translate leaf block elements only so container tags like <blockquote><p> are not duplicated
    for child in tag.find_all(_TRANSLATABLE_BLOCK_TAGS):
        if isinstance(child, Tag) and child is not tag and len(child.get_text(" ", strip=True)) >= 2:
            return False
    return True


def _parse_numbered_translations(raw: str, expected_indices: list[int]) -> dict[int, str]:
    """Parse `[n] ...` output sequentially while preserving multiline continuations and avoiding overwrites."""
    cleaned = _clean_llm_output(raw)
    if not cleaned:
        return {}

    expected_set = set(expected_indices)
    buffers: dict[int, list[str]] = {}
    current_idx: int | None = None

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MARKER_LINE_RE.match(line)
        if match:
            idx = int(match.group(1))
            rest = match.group(2).strip()
            is_sequential_new = (
                idx in expected_set
                and idx not in buffers
                and (current_idx is None or idx >= current_idx)
            )
            if is_sequential_new:
                current_idx = idx
                buffers[idx] = [rest] if rest else []
                continue
        if current_idx is not None:
            buffers[current_idx].append(line)

    result: dict[int, str] = {}
    for idx, lines in buffers.items():
        merged = "\n".join(part for part in lines if part).strip()
        if merged:
            result[idx] = merged
    return result


async def summarize_article(ai: AIClient, article: Article, *, target_lang: str = "中文") -> str:
    """Generate a concise 100-150 word editorial TL;DR for an article."""
    body = article.plain_text[:7000]
    if len(body) < 120:
        return ""
    prompt = (
        f"标题：{article.title}\n"
        f"来源：{article.source_name}\n\n"
        f"正文节选：\n{body}\n\n"
        f"请用{target_lang}提炼 2–4 句精炼导读（100–160 字），直击核心事实与结论，不要客套话。"
    )
    raw = await ai.chat(
        system="你是一位严肃纸媒的资深主编，擅长用克制、准确的语言为长文撰写导读摘要。",
        user=prompt,
        temperature=0.2,
        max_tokens=1000,
    )
    return _clean_llm_output(raw)


async def generate_frontpage_briefing(
    ai: AIClient,
    edition_name: str,
    articles: list[Article],
) -> str:
    """Synthesize a newspaper frontpage executive briefing HTML block."""
    if not articles:
        return ""
    lines: list[str] = []
    for idx, art in enumerate(articles[:35], start=1):
        snippet = (art.ai_summary or art.plain_text[:180]).replace("\n", " ")
        lines.append(f"{idx}. [{art.category}/{art.source_name}] {art.title} — {snippet}")
    joined = "\n".join(lines)
    prompt = (
        f"以下是本期《{edition_name}》收录的 {len(articles)} 篇文章清单：\n\n"
        f"{joined}\n\n"
        "请用中文撰写一份适合电子纸慢读的【今日头版综述】：\n"
        "1. 用 1 段简短编者按（60 字以内）概括今日整体脉络；\n"
        "2. 提炼 4–6 条核心看点（归并重复话题，每条带加粗小标题和 1–2 句深度解读）；\n"
        "3. 给出 2–3 篇【本期推荐精读】及简短推荐理由。\n"
        "请直接输出清晰的分段文本，不要重复输出「今日头版综述」标题，不要使用 Markdown 代码块。"
    )
    raw = await ai.chat(
        system="你是《经济学人》风格的中文日报总编，文风客观、凝练、信息密度高，适合 Kindle 阅读。",
        user=prompt,
        temperature=0.35,
        max_tokens=3200,
    )
    cleaned = _clean_llm_output(raw)
    if not cleaned:
        return ""
    cleaned = _FRONTPAGE_ECHO_RE.sub("", cleaned, count=1).strip()
    if not cleaned:
        return ""
    rendered = md_lib.markdown(cleaned, extensions=["nl2br"])
    clean_html, _, _ = sanitize_article_html(rendered)
    return clean_html


async def translate_article_bilingual(
    ai: AIClient,
    article: Article,
    *,
    target_lang: str = "简体中文",
    max_paragraphs: int | None = None,
    batch_size: int = _BILINGUAL_BATCH_SIZE,
) -> tuple[str | None, str | None]:
    """Translate title and all leaf text blocks in batches into Kindle-friendly stacked bilingual HTML."""
    if (article.language or "").strip().lower().startswith("zh"):
        return None, None

    soup = BeautifulSoup(article.clean_html or "", "lxml")
    candidate_tags = [
        tag
        for tag in soup.find_all(_TRANSLATABLE_BLOCK_TAGS)
        if isinstance(tag, Tag) and _is_translatable_block(tag)
    ]
    if not candidate_tags:
        return None, None

    selected = candidate_tags[:max_paragraphs] if max_paragraphs is not None else candidate_tags
    chunk_size = max(1, batch_size)

    translated_title: str | None = None
    body_translations: dict[int, str] = {}

    for start in range(0, len(selected), chunk_size):
        batch = selected[start : start + chunk_size]
        include_title = start == 0
        expected_indices = ([0] if include_title else []) + list(range(1, len(batch) + 1))

        numbered_lines: list[str] = []
        if include_title:
            numbered_lines.append(f"[0] {article.title}")
        for local_idx, tag in enumerate(batch, start=1):
            numbered_lines.append(f"[{local_idx}] {tag.get_text(' ', strip=True)[:1000]}")
        numbered = "\n".join(numbered_lines)

        header = "请将以下标题和各段文字" if include_title else "请将以下各段文字"
        prompt = (
            f"{header}翻译为自然流畅的{target_lang}，保持严格编号格式：\n"
            f"{numbered}\n\n"
            "输出格式严格遵守每行 `[编号] 译文`，不要省略编号，不要添加额外说明或代码块。"
        )
        raw = await ai.chat(
            system="你是专业外刊译者，译文准确、优雅、符合中文出版规范。",
            user=prompt,
            temperature=0.2,
            max_tokens=4096,
        )
        parsed = _parse_numbered_translations(raw, expected_indices)
        if include_title and parsed.get(0):
            translated_title = parsed[0]
        for local_idx in range(1, len(batch) + 1):
            if parsed.get(local_idx):
                body_translations[start + local_idx] = parsed[local_idx]

    matched_blocks = len(body_translations)
    coverage = matched_blocks / len(selected)
    if coverage < _BILINGUAL_MIN_COVERAGE:
        logger.warning(
            "ai_bilingual_low_coverage",
            article_id=getattr(article, "id", None),
            matched=matched_blocks,
            total=len(selected),
            coverage=round(coverage, 3),
        )
        return None, None

    for global_idx, tag in enumerate(selected, start=1):
        tr_text = body_translations.get(global_idx)
        if tr_text:
            tr_tag = soup.new_tag("p", attrs={"class": "bilingual-tr"})
            tr_tag.string = tr_text
            if tag.name == "li":
                tag.append(tr_tag)
            else:
                tag.insert_after(tr_tag)

    body = soup.body if soup.body else soup
    return translated_title, body.decode_contents().strip()


async def enrich_articles_with_ai(
    ai: AIClient,
    articles: list[Article],
    *,
    summarize_ids: set[int],
    bilingual_ids: set[int],
) -> None:
    """Enrich articles concurrently; errors are isolated per article."""

    async def _process(art: Article) -> None:
        if art.id in summarize_ids and not art.ai_summary:
            try:
                summary = await summarize_article(ai, art)
                if summary:
                    art.ai_summary = summary
            except Exception as exc:
                logger.warning("ai_summarize_failed", article_id=art.id, err=str(exc))
        if art.id in bilingual_ids and not art.bilingual_html:
            try:
                tr_title, bi_html = await translate_article_bilingual(ai, art)
                if tr_title:
                    art.translated_title = tr_title[:500]
                if bi_html:
                    art.bilingual_html = bi_html
            except Exception as exc:
                logger.warning("ai_translate_failed", article_id=art.id, err=str(exc))

    await asyncio.gather(*[_process(a) for a in articles])
