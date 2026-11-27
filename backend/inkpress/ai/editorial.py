from __future__ import annotations

import asyncio
import html as html_escape
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

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
_TUGUA_ITEM_RE = re.compile(r"【\s*\d{1,2}\s*】\s*([^\n\r]{4,140})")
_DIGEST_INDEX_PREFIX_RE = re.compile(r"^\s*\d+\.\s*")
_META_TAG_SUFFIX_RE = re.compile(r"\s*\[[^\]]{1,24}\]\s*$")
_USELESS_BODY_RE = re.compile(
    r"^\s*(?:原文链接|阅读原文|查看原文|\[?\d*\s*张?图片\]?|\[视频\])\s*$"
)
_ITEM_BLOCK_RE = re.compile(r"===ITEM===(.*?)===END===", re.DOTALL | re.IGNORECASE)
_IMG_TOKEN_RE = re.compile(r"\bIMG_(\d{1,3})\b", re.IGNORECASE)
_REF_TOKEN_RE = re.compile(r"\bC(\d{1,3})\b", re.IGNORECASE)
_ENTERTAINMENT_GOSSIP_RE = re.compile(
    r"(明星|穿搭|恋情|绯闻|离婚|出轨|红毯|生图|路透|应援|粉圈|塌房|机场秀|机场穿搭|素颜|热播剧|综艺|八卦)"
)
_BARE_QUESTION_PREFIX_RE = re.compile(r"^(?:如何看待|怎么看|如何评价|为什么|为何)")

_STOP_BIGRAMS = frozenset(
    {
        "如何",
        "何看",
        "看待",
        "怎么",
        "么看",
        "评价",
        "哪些",
        "什么",
        "为什",
        "最新",
        "今日",
        "消息",
        "回应",
        "表示",
        "发布",
        "披露",
        "通报",
        "值得",
        "得关",
        "关注",
        "引发",
        "热议",
        "网友",
        "问题",
        "情况",
        "事件",
        "能否",
        "是否",
        "可能",
        "中国",
        "美国",
        "全球",
        "行业",
        "市场",
        "公司",
        "企业",
        "今年",
        "明年",
        "正式",
        "宣布",
    }
)

_TRANSLATABLE_BLOCK_TAGS = ("p", "h2", "h3", "blockquote", "li")
_BILINGUAL_BATCH_SIZE = 12
_BILINGUAL_MIN_COVERAGE = 0.3


@dataclass
class DigestCandidate:
    title: str
    summary: str
    source_name: str
    category: str
    info_tag: str = ""
    canonical_url: str = ""
    images: list[str] = field(default_factory=list)


@dataclass
class CuratedDigestResult:
    body_html: str
    image_referers: dict[str, str]
    item_count: int
    used_ai: bool


@dataclass
class _ImageRegistryEntry:
    url: str
    referer: str
    alt_hint: str
    owner_cluster_ids: set[str] = field(default_factory=set)
    owner_sources: set[str] = field(default_factory=set)
    owner_topic_text: str = ""


@dataclass
class _CuratedItemBlock:
    title: str
    sources: str
    paragraphs: list[str]
    img_ids: list[str]
    ref_ids: list[str] = field(default_factory=list)


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


def _topic_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not cleaned:
        return set()
    tokens: set[str] = set()
    # Contiguous CJK runs -> character 2-grams
    for run in re.findall(r"[\u4e00-\u9fff]+", cleaned):
        if len(run) == 2 and run not in _STOP_BIGRAMS:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                bg = run[i : i + 2]
                if bg not in _STOP_BIGRAMS:
                    tokens.add(bg)
    # Latin words & numeric identifiers (e.g. yu7, 301, 6g, gpt, oceanbase)
    for word in re.findall(r"[a-z0-9]{2,}", cleaned):
        if not word.isdigit() or len(word) >= 3:
            tokens.add(word)
    return tokens


def compute_topic_similarity(text_a: str, text_b: str) -> float:
    """Compute hybrid Dice + containment + multi-entity overlap similarity across headlines."""
    toks_a = _topic_tokens(text_a)
    toks_b = _topic_tokens(text_b)
    if not toks_a or not toks_b:
        return 0.0
    shared = len(toks_a & toks_b)
    if shared == 0:
        return 0.0
    dice = (2.0 * shared) / (len(toks_a) + len(toks_b))
    min_side = min(len(toks_a), len(toks_b))
    containment = shared / min_side if min_side >= 3 else dice
    # Long Chinese headlines (~25 bigrams) sharing >= 4 distinct non-stopword bigrams
    # (e.g. person name + domain + action) represent the same underlying news event
    entity_overlap = 0.0
    if shared >= 5:
        entity_overlap = min(0.85, 0.38 + (shared - 4) * 0.07)
    elif shared == 4 and min_side <= 28:
        entity_overlap = 0.37
    return max(dice, containment * 0.78, entity_overlap)


def is_duplicate_topic(
    candidate_title: str,
    reference_topics: Sequence[str],
    *,
    threshold: float = 0.36,
) -> bool:
    if not candidate_title or not reference_topics:
        return False
    return any(compute_topic_similarity(candidate_title, ref) >= threshold for ref in reference_topics)


def extract_tugua_topics(plain_text: str, title: str = "") -> list[str]:
    """Extract numbered item headlines (`【1】...`) from a 喷嚏图卦 issue as deduplication anchors."""
    topics: list[str] = []
    if title:
        cleaned_main = re.sub(r"^【[^】]*】\s*", "", title).strip()
        if len(cleaned_main) >= 4:
            topics.append(cleaned_main)
    for match in _TUGUA_ITEM_RE.finditer(plain_text or ""):
        item_line = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(item_line) >= 4:
            topics.append(item_line[:120])
    return topics


def _extract_article_images(clean_html: str, lead_image_url: str | None, max_imgs: int = 2) -> list[str]:
    urls: list[str] = []
    if lead_image_url and lead_image_url.startswith(("http://", "https://")):
        urls.append(lead_image_url.strip())
    if clean_html:
        soup = BeautifulSoup(clean_html, "lxml")
        for img in soup.find_all("img"):
            src = str(img.get("src") or "").strip()
            if src.startswith(("http://", "https://")) and src not in urls:
                urls.append(src)
                if len(urls) >= max_imgs:
                    break
    return urls[:max_imgs]


def _clean_candidate_summary(raw: str, title: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    text = re.sub(r"\s*(?:原文链接|阅读原文)\s*$", "", text).strip()
    if not text or _USELESS_BODY_RE.match(text):
        return ""
    if text == title.strip() or text == f"{title.strip()}（来源":
        return ""
    # Strip duplicated leading title if body begins with exact title
    if text.startswith(title.strip()) and len(text) > len(title.strip()) + 12:
        text = text[len(title.strip()) :].lstrip(" ：:|｜-—")
    return text[:800]


def _cap_digest_bundle_items(
    bundle: list[DigestCandidate], max_per_digest: int
) -> list[DigestCandidate]:
    if max_per_digest <= 0 or len(bundle) <= max_per_digest:
        return bundle
    # Prefer rich items (with summary or image) while preserving original hotlist ranking order
    scored_indices = sorted(
        range(len(bundle)),
        key=lambda i: (bool(bundle[i].summary or bundle[i].images), -i),
        reverse=True,
    )
    chosen = set(scored_indices[:max_per_digest])
    return [item for idx, item in enumerate(bundle) if idx in chosen]


def extract_candidates_from_articles(
    articles: Sequence[Article],
    *,
    max_per_digest: int = 6,
) -> list[DigestCandidate]:
    """Unpack both `newsnow_digest` bundles and standalone short/news articles into DigestCandidates."""
    candidates: list[DigestCandidate] = []
    for art in articles:
        meta = art.extra_meta if isinstance(art.extra_meta, dict) else {}
        structured = meta.get("digest_items")
        if isinstance(structured, list) and structured:
            bundle_cands: list[DigestCandidate] = []
            for item in structured:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("title") or "").strip()
                if not t:
                    continue
                s = _clean_candidate_summary(str(item.get("summary") or ""), t)
                img = str(item.get("image_url") or "").strip()
                url = str(item.get("url") or "").strip()
                bundle_cands.append(
                    DigestCandidate(
                        title=t,
                        summary=s,
                        source_name=art.source_name or "热榜",
                        category=art.category or "综合",
                        info_tag=str(item.get("info_tag") or "").strip(),
                        canonical_url=url,
                        images=[img] if img.startswith(("http://", "https://")) else [],
                    )
                )
            candidates.extend(_cap_digest_bundle_items(bundle_cands, max_per_digest))
            continue

        if "<div class=\"digest-item\"" in (art.clean_html or ""):
            soup = BeautifulSoup(art.clean_html, "lxml")
            html_bundle: list[DigestCandidate] = []
            for div in soup.find_all("div", class_="digest-item"):
                if not isinstance(div, Tag):
                    continue
                h3 = div.find("h3")
                if not isinstance(h3, Tag):
                    continue
                info_tag = ""
                badge = h3.find("span", class_="meta-tag")
                if isinstance(badge, Tag):
                    info_tag = badge.get_text(strip=True).strip("[]")
                    badge.decompose()
                raw_t = _DIGEST_INDEX_PREFIX_RE.sub("", h3.get_text(" ", strip=True)).strip()
                raw_t = _META_TAG_SUFFIX_RE.sub("", raw_t).strip()
                if not raw_t:
                    continue
                paras: list[str] = []
                for p in div.find_all("p"):
                    if not isinstance(p, Tag):
                        continue
                    classes = p.get("class") or []
                    if "source-link" in classes:
                        continue
                    ptxt = p.get_text(" ", strip=True)
                    if ptxt and not _USELESS_BODY_RE.match(ptxt):
                        paras.append(ptxt)
                s = _clean_candidate_summary(" ".join(paras), raw_t)
                imgs = _extract_article_images(str(div), None, max_imgs=1)
                link_a = div.find("a", href=True)
                item_url = str(link_a.get("href") or "").strip() if isinstance(link_a, Tag) else ""
                html_bundle.append(
                    DigestCandidate(
                        title=raw_t,
                        summary=s,
                        source_name=art.source_name or "热榜",
                        category=art.category or "综合",
                        info_tag=info_tag,
                        canonical_url=item_url,
                        images=imgs,
                    )
                )
            candidates.extend(_cap_digest_bundle_items(html_bundle, max_per_digest))
            continue

        # Regular extracted news / RSS article
        summary = _clean_candidate_summary(art.ai_summary or art.plain_text, art.title)
        imgs = _extract_article_images(art.clean_html, art.lead_image_url, max_imgs=2)
        candidates.append(
            DigestCandidate(
                title=art.title.strip(),
                summary=summary,
                source_name=art.source_name or "精选",
                category=art.category or "综合",
                canonical_url=art.canonical_url,
                images=imgs,
            )
        )
    return candidates


def cluster_and_filter_candidates(
    candidates: Sequence[DigestCandidate],
    *,
    excluded_topics: Sequence[str] = (),
    max_clusters: int = 56,
) -> list[DigestCandidate]:
    """Deduplicate against excluded topics (e.g. 喷嚏图卦), merge cross-source duplicates, and balance categories."""
    clusters: list[DigestCandidate] = []

    for cand in candidates:
        if not cand.title:
            continue
        if is_duplicate_topic(cand.title, excluded_topics, threshold=0.35):
            continue

        # Check if cand matches an existing cluster
        matched_cluster: DigestCandidate | None = None
        best_sim = 0.0
        for existing in clusters:
            sim = compute_topic_similarity(cand.title, existing.title)
            if sim >= 0.36 and sim > best_sim:
                best_sim = sim
                matched_cluster = existing

        if matched_cluster is not None:
            if cand.source_name and cand.source_name not in matched_cluster.source_name:
                matched_cluster.source_name = f"{matched_cluster.source_name} · {cand.source_name}"
            if cand.summary and cand.summary not in matched_cluster.summary:
                if not matched_cluster.summary:
                    matched_cluster.summary = cand.summary
                elif len(matched_cluster.summary) < 600:
                    matched_cluster.summary = f"{matched_cluster.summary}\n补充（{cand.source_name}）：{cand.summary[:320]}"
            for img in cand.images:
                if img not in matched_cluster.images and len(matched_cluster.images) < 3:
                    matched_cluster.images.append(img)
                    if not matched_cluster.canonical_url and cand.canonical_url:
                        matched_cluster.canonical_url = cand.canonical_url
            if not matched_cluster.info_tag and cand.info_tag:
                matched_cluster.info_tag = cand.info_tag
        else:
            clusters.append(
                DigestCandidate(
                    title=cand.title,
                    summary=cand.summary,
                    source_name=cand.source_name,
                    category=cand.category,
                    info_tag=cand.info_tag,
                    canonical_url=cand.canonical_url,
                    images=list(cand.images),
                )
            )

    # Drop hollow gossip/question tags while keeping factual CJK & English declarative dispatches
    substantive: list[DigestCandidate] = []
    for cl in clusters:
        title_clean = cl.title.strip()
        has_real_body = len(cl.summary.strip()) >= 16
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", title_clean))
        is_question = title_clean.endswith(("?", "？")) or bool(
            _BARE_QUESTION_PREFIX_RE.search(title_clean)
        )
        is_gossip = bool(_ENTERTAINMENT_GOSSIP_RE.search(title_clean))
        is_multi_source = " · " in cl.source_name

        if has_real_body:
            substantive.append(cl)
            continue
        if is_gossip:
            continue
        if is_question and not is_multi_source:
            continue

        is_cjk_flash = has_cjk and len(title_clean) >= 9
        is_en_flash = (not has_cjk) and len(title_clean.split()) >= 4 and len(title_clean) >= 20
        if is_cjk_flash or is_en_flash:
            substantive.append(cl)

    # Sort within each category by editorial signal, then interleave categories round-robin
    # so 15+ sources across 国际/财经/科技/综合/文化 all get balanced representation
    substantive.sort(
        key=lambda c: (
            " · " in c.source_name,
            bool(c.images),
            min(len(c.summary), 300) >= 60,
        ),
        reverse=True,
    )
    by_cat: dict[str, list[DigestCandidate]] = {}
    for item in substantive:
        by_cat.setdefault(item.category or "综合", []).append(item)

    balanced: list[DigestCandidate] = []
    max_cat_depth = max((len(v) for v in by_cat.values()), default=0)
    for depth in range(max_cat_depth):
        for cat_list in by_cat.values():
            if depth < len(cat_list):
                balanced.append(cat_list[depth])
                if len(balanced) >= max_clusters:
                    return balanced
    return balanced[:max_clusters]


def _is_image_owned_by_item(
    entry: _ImageRegistryEntry,
    item: _CuratedItemBlock,
) -> bool:
    """Verify an LLM-requested IMG_xx genuinely belongs to the merged item's candidate cluster(s)."""
    item_text = f"{item.title} {' '.join(item.paragraphs)}"
    topic_sim = compute_topic_similarity(item_text, entry.owner_topic_text)

    item_sources = {
        s.strip().lower()
        for s in re.split(r"[·,/｜|、]+", item.sources)
        if s.strip()
    }
    owner_sources_lower = {s.lower() for s in entry.owner_sources if s}
    source_overlap = bool(
        item_sources
        and owner_sources_lower
        and any(
            isrc in osrc or osrc in isrc
            for isrc in item_sources
            for osrc in owner_sources_lower
        )
    )

    if item.ref_ids and (entry.owner_cluster_ids & set(item.ref_ids)):
        return topic_sim >= 0.08 or (source_overlap and topic_sim >= 0.03)

    return (source_overlap and topic_sim >= 0.12) or topic_sim >= 0.22


def _render_tugua_digest_html(
    items: list[_CuratedItemBlock],
    img_registry: dict[str, _ImageRegistryEntry],
    *,
    trusted_images: bool = False,
) -> tuple[str, dict[str, str]]:
    """Render curated items into clean 喷嚏图卦-style XHTML while rejecting mismatched images."""
    blocks: list[str] = []
    referers: dict[str, str] = {}
    used_img_urls: set[str] = set()

    for idx, item in enumerate(items, start=1):
        safe_title = html_escape.escape(item.title.strip())
        src_badge = (
            f' <span class="meta-tag">｜{html_escape.escape(item.sources.strip())}</span>'
            if item.sources.strip()
            else ""
        )
        part_lines = [
            '<div class="tugua-item">',
            f'<p class="tugua-title no-indent"><strong>【{idx}】{safe_title}</strong>{src_badge}</p>',
        ]
        for para in item.paragraphs:
            cleaned_p = para.strip()
            if cleaned_p:
                part_lines.append(f"<p>{html_escape.escape(cleaned_p)}</p>")

        attached = 0
        for raw_id in item.img_ids:
            norm_id = raw_id.upper()
            entry = img_registry.get(norm_id)
            if entry is None:
                continue
            if not trusted_images and not _is_image_owned_by_item(entry, item):
                logger.info(
                    "curated_digest_mismatched_image_dropped",
                    img_id=norm_id,
                    item_title=item.title[:60],
                    img_owners=sorted(entry.owner_cluster_ids),
                    item_refs=item.ref_ids,
                )
                continue
            if entry.url in used_img_urls:
                continue
            used_img_urls.add(entry.url)
            if entry.referer:
                referers[entry.url] = entry.referer
            safe_src = html_escape.escape(entry.url, quote=True)
            safe_alt = html_escape.escape((entry.alt_hint or item.title)[:100], quote=True)
            part_lines.append(
                f'<figure class="tugua-figure"><img src="{safe_src}" alt="{safe_alt}"/></figure>'
            )
            attached += 1
            if attached >= 2:
                break

        part_lines.append("</div>")
        blocks.append("\n".join(part_lines))

    raw_html = "\n".join(blocks)
    sanitized_html, _, _ = sanitize_article_html(raw_html)
    return sanitized_html or raw_html, referers


def _parse_curated_llm_blocks(
    raw: str,
) -> list[_CuratedItemBlock]:
    cleaned = _clean_llm_output(raw)
    if not cleaned:
        return []

    parsed: list[_CuratedItemBlock] = []
    matches = list(_ITEM_BLOCK_RE.finditer(cleaned))
    for m in matches:
        block = m.group(1).strip()
        title = ""
        sources = ""
        img_ids: list[str] = []
        ref_ids: list[str] = []
        body_lines: list[str] = []
        in_body = False

        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            upper = line.upper()
            if not in_body and upper.startswith("TITLE:"):
                title = re.sub(r"^【\s*\d+\s*】\s*", "", line[6:].strip()).strip("* ")
                continue
            if not in_body and upper.startswith("SOURCES:"):
                sources = line[8:].strip()
                continue
            if not in_body and upper.startswith("REFS:"):
                ref_ids = [f"C{int(n):02d}" for n in _REF_TOKEN_RE.findall(line)]
                continue
            if not in_body and upper.startswith("IMAGES:"):
                img_ids = [f"IMG_{int(n):02d}" for n in _IMG_TOKEN_RE.findall(line)]
                continue
            if not in_body and upper.startswith("BODY:"):
                in_body = True
                rest = line[5:].strip()
                if rest:
                    body_lines.append(rest)
                continue
            if in_body:
                body_lines.append(line)

        if title and body_lines:
            parsed.append(
                _CuratedItemBlock(
                    title=title,
                    sources=sources,
                    paragraphs=body_lines,
                    img_ids=img_ids,
                    ref_ids=ref_ids,
                )
            )

    return parsed


def _build_fallback_digest(
    clusters: Sequence[DigestCandidate],
    img_registry: dict[str, _ImageRegistryEntry],
    url_to_img_id: dict[str, str],
    *,
    max_items: int,
) -> CuratedDigestResult:
    items: list[_CuratedItemBlock] = []
    for cl in clusters[:max_items]:
        body = cl.summary.strip() or cl.title.strip()
        paras = [p.strip() for p in body.split("\n") if p.strip()][:2]
        ids = [url_to_img_id[u] for u in cl.images if u in url_to_img_id][:2]
        items.append(
            _CuratedItemBlock(
                title=cl.title,
                sources=cl.source_name,
                paragraphs=paras,
                img_ids=ids,
            )
        )
    if not items:
        return CuratedDigestResult(body_html="", image_referers={}, item_count=0, used_ai=False)
    html_out, refs = _render_tugua_digest_html(items, img_registry, trusted_images=True)
    return CuratedDigestResult(
        body_html=html_out,
        image_referers=refs,
        item_count=len(items),
        used_ai=False,
    )


async def curate_illustrated_digest(
    ai: AIClient | None,
    candidates: Sequence[DigestCandidate],
    *,
    excluded_topics: Sequence[str] = (),
    max_items: int = 15,
) -> CuratedDigestResult:
    """Synthesize multi-source hotlists, news dispatches, and RSS briefs into one deduplicated,
    喷嚏图卦-style illustrated chapter (`【1】…【N】` with inline curated photos/charts)."""
    clusters = cluster_and_filter_candidates(
        candidates,
        excluded_topics=excluded_topics,
        max_clusters=56,
    )
    if not clusters:
        return CuratedDigestResult(body_html="", image_referers={}, item_count=0, used_ai=False)

    # Build verified image registry with candidate ownership metadata across clusters
    img_registry: dict[str, _ImageRegistryEntry] = {}
    url_to_img_id: dict[str, str] = {}
    img_counter = 1
    for idx, cl in enumerate(clusters, start=1):
        cid = f"C{idx:02d}"
        cl_sources = {s.strip() for s in cl.source_name.split("·") if s.strip()}
        cl_topic = f"{cl.title} {cl.summary[:300]}".strip()
        for img_url in cl.images:
            if img_url not in url_to_img_id:
                if img_counter > 96:
                    continue
                img_id = f"IMG_{img_counter:02d}"
                img_counter += 1
                url_to_img_id[img_url] = img_id
                img_registry[img_id] = _ImageRegistryEntry(
                    url=img_url,
                    referer=cl.canonical_url,
                    alt_hint=cl.title,
                    owner_cluster_ids={cid},
                    owner_sources=set(cl_sources),
                    owner_topic_text=cl_topic,
                )
            else:
                existing_entry = img_registry[url_to_img_id[img_url]]
                existing_entry.owner_cluster_ids.add(cid)
                existing_entry.owner_sources.update(cl_sources)
                existing_entry.owner_topic_text = f"{existing_entry.owner_topic_text} {cl_topic}".strip()

    if ai is None:
        return _build_fallback_digest(clusters, img_registry, url_to_img_id, max_items=max_items)

    excl_block = ""
    if excluded_topics:
        excl_lines = "\n".join(f"- {t}" for t in list(excluded_topics)[:45])
        excl_block = (
            "【已收录于《喷嚏图卦》或独立长文的话题（严格排重基准——凡属同一事件一律跳过，切勿重复）】\n"
            f"{excl_lines}\n\n"
        )

    cand_blocks: list[str] = []
    for idx, cl in enumerate(clusters, start=1):
        c_imgs = [url_to_img_id[u] for u in cl.images if u in url_to_img_id]
        img_note = f"可用配图: {', '.join(c_imgs)}" if c_imgs else "可用配图: NONE"
        tag_note = f" ({cl.info_tag})" if cl.info_tag else ""
        excerpt = cl.summary[:450] if cl.summary else "（快讯简报）"
        cand_blocks.append(
            f"[C{idx:02d}] 来源: {cl.source_name}{tag_note} | 分类: {cl.category} | {img_note}\n"
            f"标题: {cl.title}\n"
            f"内容摘要: {excerpt}"
        )

    target_min = min(10, len(clusters))
    target_max = min(max_items, len(clusters))
    prompt = (
        f"{excl_block}"
        f"以下是今日来自全网多个订阅源（国际、财经、科技/AI、社会、文化）的 {len(clusters)} 条候选资讯：\n\n"
        + "\n\n".join(cand_blocks)
        + "\n\n"
        f"请参考《喷嚏图卦》的经典图文阅读体验，将上述资讯去重整合、精选编排为 {target_min}～{target_max} 条高质量【全网图文辑要】：\n"
        "1. **严格去重与跨源合并**：凡与上方【严格排重基准】属于同一事件的话题一律剔除；候选池中讨论同一事件或关联议题的多条资讯必须合并为同一条，综合多方核心事实、关键数据与热议焦点，并在 SOURCES 中列出合并的来源。\n"
        "2. **多领域均衡精选**：兼顾国际时政、宏观与资本市场、AI与前沿科技、社会公共议题及高口碑文化书影；剔除娱乐八卦、促销软文、琐碎水帖及缺乏实质事实支撑的空洞条目；严禁脱离给定材料编造事实。\n"
        "3. **喷嚏图卦式精炼表达**：每条拟定一个直击要害的短标题（不带序号），正文写 1～2 段克制、高信息密度的事实与脉络综述（每条 100～220 字，直入主题，不写套话）。\n"
        "4. **引用溯源与配图严禁串行**：在 `REFS:` 填入本条实际引用的候选编号（如 `C01, C05`）；`IMAGES:` 仅允许填写 `REFS` 对应候选所拥有的 `IMG_xx`（最多 2 张，无图填 `NONE`，严禁借用其他候选条目的配图或编造编号）。\n\n"
        "请严格按以下标记块格式输出每一条（不要输出 Markdown 代码块或任何额外前后缀）：\n"
        "===ITEM===\n"
        "TITLE: 一句话核心标题\n"
        "SOURCES: 华尔街见闻 · 知乎热榜\n"
        "REFS: C01, C05\n"
        "IMAGES: IMG_01\n"
        "BODY:\n"
        "第一段核心事实、关键数据与事件脉络……\n"
        "===END==="
    )

    try:
        raw = await ai.chat(
            system=(
                "你是《喷嚏图卦》与《经济学人》融合风格的资深图文日报总编，"
                "擅长跨源去重、提炼高密度事实并精准搭配新闻配图，专为 Kindle 电子纸打造沉浸式阅读体验。"
            ),
            user=prompt,
            temperature=0.25,
            max_tokens=6000,
        )
        parsed_items = _parse_curated_llm_blocks(raw)
        if len(parsed_items) >= min(2, len(clusters)):
            html_out, refs = _render_tugua_digest_html(parsed_items[:max_items], img_registry)
            return CuratedDigestResult(
                body_html=html_out,
                image_referers=refs,
                item_count=min(len(parsed_items), max_items),
                used_ai=True,
            )
        logger.warning(
            "ai_curated_digest_parse_fallback",
            parsed_count=len(parsed_items),
            candidate_count=len(clusters),
        )
    except Exception as exc:
        logger.warning("ai_curated_digest_failed", err=str(exc))

    return _build_fallback_digest(clusters, img_registry, url_to_img_id, max_items=max_items)


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
