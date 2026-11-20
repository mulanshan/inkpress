from __future__ import annotations

import asyncio
import html as html_escape

from bs4 import BeautifulSoup

from inkpress.ai.client import AIClient
from inkpress.db.models import Article
from inkpress.logging import get_logger

logger = get_logger(__name__)


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
    return await ai.chat(
        system="你是一位严肃纸媒的资深主编，擅长用克制、准确的语言为长文撰写导读摘要。",
        user=prompt,
        temperature=0.2,
        max_tokens=320,
    )


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
        "请直接输出清晰的分段文本，不要使用 Markdown 代码块。"
    )
    raw = await ai.chat(
        system="你是《经济学人》风格的中文日报总编，文风客观、凝练、信息密度高，适合 Kindle 阅读。",
        user=prompt,
        temperature=0.35,
        max_tokens=1100,
    )
    if not raw:
        return ""
    paragraphs: list[str] = []
    for block in raw.splitlines():
        line = block.strip()
        if not line:
            continue
        if line.startswith(("#", "##", "###")):
            heading = html_escape.escape(line.lstrip("# ").strip())
            paragraphs.append(f"<h2>{heading}</h2>")
        elif line.startswith(("- ", "• ", "* ")):
            item = html_escape.escape(line[2:].strip())
            paragraphs.append(f'<p class="brief-bullet">• {item}</p>')
        else:
            paragraphs.append(f"<p>{html_escape.escape(line)}</p>")
    return "\n".join(paragraphs)


async def translate_article_bilingual(
    ai: AIClient,
    article: Article,
    *,
    target_lang: str = "简体中文",
    max_paragraphs: int = 24,
) -> tuple[str | None, str | None]:
    """Translate title and top paragraphs into clean Kindle-friendly stacked bilingual HTML."""
    soup = BeautifulSoup(article.clean_html or "", "lxml")
    para_tags = [
        p
        for p in soup.find_all(["p", "h2", "h3", "blockquote"])
        if len(p.get_text(strip=True)) >= 20
    ]
    if not para_tags:
        return None, None

    selected = para_tags[:max_paragraphs]
    numbered = "\n".join(
        f"[{i}] {p.get_text(' ', strip=True)[:800]}" for i, p in enumerate(selected, start=1)
    )
    prompt = (
        f"请将以下标题和各段文字翻译为自然流畅的{target_lang}，保持严格编号格式：\n"
        f"[0] {article.title}\n"
        f"{numbered}\n\n"
        "输出格式严格遵守每行 `[编号] 译文`，不要省略编号，不要添加额外说明。"
    )
    raw = await ai.chat(
        system="你是专业外刊译者，译文准确、优雅、符合中文出版规范。",
        user=prompt,
        temperature=0.2,
        max_tokens=2800,
    )
    translations: dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        close = line.find("]")
        if close <= 1:
            continue
        idx_str = line[1:close].strip()
        if idx_str.isdigit():
            translations[int(idx_str)] = line[close + 1 :].strip()

    translated_title = translations.get(0)
    for i, tag in enumerate(selected, start=1):
        tr_text = translations.get(i)
        if tr_text:
            tr_tag = soup.new_tag("p", attrs={"class": "bilingual-tr"})
            tr_tag.string = tr_text
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
                art.ai_summary = await summarize_article(ai, art)
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
