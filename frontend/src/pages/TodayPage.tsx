import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Bookmark,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  ExternalLink,
  Languages,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  X,
} from "lucide-react";
import { apiRequest, formatRelativeTime, isSafeHttpUrl, mediaProxyUrl } from "../lib/api";
import { useReaderAppearance } from "../lib/theme";
import type { ArticleDetail, ArticleSummary } from "../lib/types";
import { ReaderSettingsBar } from "../components/ReaderSettingsBar";
import { useToast } from "../components/Toast";

interface ArticlesResponse {
  items: ArticleSummary[];
  categories: { name: string; count: number }[];
  total?: number;
}

const PAGE_SIZE = 40;

function rewriteArticleHtmlImages(html: string, refererUrl?: string): string {
  if (!html) return "";
  try {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const imgs = doc.querySelectorAll("img");
    imgs.forEach((img) => {
      const rawSrc = img.getAttribute("src") || "";
      if (isSafeHttpUrl(rawSrc)) {
        img.setAttribute("src", mediaProxyUrl(rawSrc, refererUrl));
        img.removeAttribute("srcset");
      }
      if (!img.getAttribute("loading")) {
        img.setAttribute("loading", "lazy");
      }
    });
    return doc.body.innerHTML;
  } catch {
    return html;
  }
}

export function TodayPage({ onGoSources }: { onGoSources: () => void }) {
  const qc = useQueryClient();
  const { notify } = useToast();
  const { fontFamily, fontSize, lineHeight } = useReaderAppearance();

  const [searchInput, setSearchInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState<string>("");
  const [starredOnly, setStarredOnly] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showBilingual, setShowBilingual] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [pushingArticleId, setPushingArticleId] = useState<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(searchInput.trim()), 260);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  const listQuery = useInfiniteQuery({
    queryKey: ["articles", debouncedQuery, activeCategory, starredOnly, unreadOnly],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      if (debouncedQuery) params.set("q", debouncedQuery);
      if (activeCategory) params.set("category", activeCategory);
      if (starredOnly) params.set("starred", "true");
      if (unreadOnly) params.set("unread_only", "true");
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(pageParam));
      return apiRequest<ArticlesResponse>(`/api/v1/articles?${params.toString()}`);
    },
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0);
      if (typeof lastPage.total === "number") {
        return loaded < lastPage.total && lastPage.items.length > 0 ? loaded : undefined;
      }
      return lastPage.items.length >= PAGE_SIZE ? loaded : undefined;
    },
  });

  const detailQuery = useQuery({
    queryKey: ["article-detail", selectedId],
    enabled: selectedId !== null,
    queryFn: () => apiRequest<{ item: ArticleDetail }>(`/api/v1/articles/${selectedId}`),
  });

  const items = useMemo(() => {
    const seen = new Set<number>();
    const merged: ArticleSummary[] = [];
    for (const page of listQuery.data?.pages ?? []) {
      for (const item of page.items) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          merged.push(item);
        }
      }
    }
    return merged;
  }, [listQuery.data]);

  const firstPage = listQuery.data?.pages[0];
  const categories = firstPage?.categories ?? [];
  const totalCount = typeof firstPage?.total === "number" ? firstPage.total : items.length;
  const allArticlesTotal =
    !activeCategory && !starredOnly && !unreadOnly && !debouncedQuery
      ? totalCount
      : categories.reduce((sum, cat) => sum + cat.count, 0) || totalCount;
  const activeArticle = detailQuery.data?.item ?? null;

  // Scroll to top whenever entering or switching article in reader view
  useEffect(() => {
    if (selectedId !== null) {
      window.scrollTo({ top: 0 });
    }
  }, [selectedId]);

  // Mark read on open
  useEffect(() => {
    if (activeArticle && !activeArticle.is_read) {
      apiRequest(`/api/v1/articles/${activeArticle.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_read: true }),
      })
        .then(() => qc.invalidateQueries({ queryKey: ["articles"] }))
        .catch(() => {});
    }
  }, [activeArticle, qc]);

  // Keyboard shortcuts: / to search, Esc to exit reader, j/k to browse
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable
      ) {
        if (e.key === "Escape") target?.blur();
        return;
      }
      if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape" && selectedId !== null) {
        setSelectedId(null);
      } else if (e.key === "j" && items.length > 0) {
        const idx = items.findIndex((i) => i.id === selectedId);
        const next = items[Math.min(items.length - 1, idx + 1)];
        if (next) setSelectedId(next.id);
      } else if (e.key === "k" && items.length > 0 && selectedId !== null) {
        const idx = items.findIndex((i) => i.id === selectedId);
        const prev = items[Math.max(0, idx - 1)];
        if (prev) setSelectedId(prev.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selectedId]);

  const rawHtmlContent =
    showBilingual && activeArticle?.bilingual_html
      ? activeArticle.bilingual_html
      : activeArticle?.clean_html || "";

  const proxiedHtmlContent = useMemo(
    () => rewriteArticleHtmlImages(rawHtmlContent, activeArticle?.canonical_url),
    [rawHtmlContent, activeArticle?.canonical_url],
  );

  const toggleStar = async (article: ArticleSummary) => {
    try {
      await apiRequest(`/api/v1/articles/${article.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_starred: !article.is_starred }),
      });
      await qc.invalidateQueries({ queryKey: ["articles"] });
      await qc.invalidateQueries({ queryKey: ["article-detail", article.id] });
      notify(article.is_starred ? "已取消收藏" : "已加入收藏", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "操作失败", "error");
    }
  };

  const pushToKindle = async (articleId: number) => {
    if (pushingArticleId !== null || busyAction !== null) return;
    setBusyAction("push");
    setPushingArticleId(articleId);
    try {
      await apiRequest(`/api/v1/articles/${articleId}/push`, { method: "POST" });
      notify("已打包为 EPUB 并推送到 Kindle", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "推送失败", "error");
    } finally {
      setBusyAction(null);
      setPushingArticleId(null);
    }
  };

  const runAiEnrich = async (articleId: number, mode: "summary" | "bilingual") => {
    setBusyAction(mode);
    try {
      await apiRequest(`/api/v1/articles/${articleId}/ai-enrich?mode=${mode}`, {
        method: "POST",
      });
      await qc.invalidateQueries({ queryKey: ["article-detail", articleId] });
      await qc.invalidateQueries({ queryKey: ["articles"] });
      notify(mode === "summary" ? "AI 导读已生成" : "双语对照已生成", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "AI 处理失败", "error");
    } finally {
      setBusyAction(null);
    }
  };

  const todayLabel = new Date().toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  });

  // Distraction-free immersive article reader view
  if (selectedId !== null) {
    const currentIndex = items.findIndex((i) => i.id === selectedId);
    const prevArticle = currentIndex > 0 ? items[currentIndex - 1] : null;
    const nextArticle =
      currentIndex >= 0 && currentIndex < items.length - 1 ? items[currentIndex + 1] : null;
    const displayTranslatedTitle = Boolean(showBilingual && activeArticle?.translated_title);

    return (
      <div className="max-w-3xl mx-auto pb-20">
        <div
          className="sticky top-[49px] md:top-0 z-20 flex flex-wrap items-center justify-between gap-2 py-3 mb-8 border-b backdrop-blur-sm"
          style={{
            backgroundColor: "color-mix(in srgb, var(--bg-canvas) 92%, transparent)",
            borderColor: "var(--border-hairline)",
          }}
        >
          <button
            type="button"
            onClick={() => setSelectedId(null)}
            className="inline-flex items-center gap-1.5 text-sm font-medium px-2.5 py-1.5 rounded-lg cursor-pointer hover:opacity-80"
            style={{ color: "var(--fg-secondary)", backgroundColor: "var(--bg-subtle)" }}
          >
            <ArrowLeft className="w-4 h-4" />
            <span>返回今日刊</span>
          </button>

          <div className="flex flex-wrap items-center gap-2">
            {activeArticle && (
              <>
                <button
                  type="button"
                  disabled={busyAction !== null}
                  onClick={() => runAiEnrich(activeArticle.id, "summary")}
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border cursor-pointer disabled:opacity-50"
                  style={{
                    borderColor: "var(--border-hairline)",
                    backgroundColor: "var(--bg-surface)",
                  }}
                >
                  <Sparkles className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />
                  <span>{busyAction === "summary" ? "生成中…" : "AI 导读"}</span>
                </button>

                <button
                  type="button"
                  disabled={busyAction !== null}
                  onClick={() => {
                    if (activeArticle.bilingual_html) {
                      setShowBilingual((v) => !v);
                    } else {
                      runAiEnrich(activeArticle.id, "bilingual");
                    }
                  }}
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border cursor-pointer disabled:opacity-50"
                  style={{
                    borderColor: "var(--border-hairline)",
                    backgroundColor:
                      activeArticle.bilingual_html && showBilingual
                        ? "var(--accent-soft)"
                        : "var(--bg-surface)",
                  }}
                >
                  <Languages className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />
                  <span>
                    {busyAction === "bilingual"
                      ? "翻译中…"
                      : activeArticle.bilingual_html
                        ? showBilingual
                          ? "双语中"
                          : "切回双语"
                        : "双语翻译"}
                  </span>
                </button>

                <button
                  type="button"
                  onClick={() => toggleStar(activeArticle)}
                  className="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer"
                  style={{
                    borderColor: "var(--border-hairline)",
                    backgroundColor: activeArticle.is_starred
                      ? "var(--accent-soft)"
                      : "var(--bg-surface)",
                    color: activeArticle.is_starred ? "var(--accent)" : "var(--fg-secondary)",
                  }}
                  title={activeArticle.is_starred ? "取消收藏" : "加入收藏"}
                >
                  <Bookmark className="w-3.5 h-3.5" />
                </button>

                <button
                  type="button"
                  disabled={busyAction !== null || pushingArticleId !== null}
                  onClick={() => pushToKindle(activeArticle.id)}
                  className="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50"
                  style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                >
                  <Send className="w-3.5 h-3.5" />
                  <span>{busyAction === "push" ? "推送中…" : "推至 Kindle"}</span>
                </button>
              </>
            )}
          </div>
        </div>

        <div className="mb-8 flex justify-end">
          <ReaderSettingsBar />
        </div>

        {detailQuery.isError ? (
          <div
            className="rounded-2xl border p-10 text-center space-y-4"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-danger)",
            }}
          >
            <AlertCircle className="w-7 h-7 mx-auto" style={{ color: "var(--fg-danger)" }} />
            <div>
              <p className="font-editorial text-xl font-semibold mb-1">正文加载失败</p>
              <p className="text-xs" style={{ color: "var(--fg-danger)" }}>
                {detailQuery.error instanceof Error ? detailQuery.error.message : "无法获取文章详情"}
              </p>
            </div>
            <div className="flex items-center justify-center gap-2">
              <button
                type="button"
                onClick={() => detailQuery.refetch()}
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                重试加载
              </button>
              <button
                type="button"
                onClick={() => setSelectedId(null)}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
              >
                返回今日刊
              </button>
            </div>
          </div>
        ) : detailQuery.isLoading || !activeArticle ? (
          <div className="py-24 text-center text-sm" style={{ color: "var(--fg-muted)" }}>
            正在载入正文…
          </div>
        ) : (
          <article className="mx-auto" style={{ maxWidth: "68ch" }}>
            <div className="mb-6">
              <div
                className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-wider mb-3"
                style={{ color: "var(--accent)" }}
              >
                <span>{activeArticle.category}</span>
                <span>·</span>
                <span>{activeArticle.source_name || "订阅"}</span>
                <span style={{ color: "var(--fg-muted)" }}>·</span>
                <span style={{ color: "var(--fg-muted)" }}>
                  约 {activeArticle.reading_time_min} 分钟阅读（{activeArticle.word_count} 字）
                </span>
              </div>

              <h1 className="font-editorial text-3xl sm:text-4xl font-semibold leading-snug mb-3">
                {displayTranslatedTitle ? activeArticle.translated_title : activeArticle.title}
              </h1>
              {displayTranslatedTitle && (
                <p className="text-base mb-3" style={{ color: "var(--fg-secondary)" }}>
                  {activeArticle.title}
                </p>
              )}

              <div
                className="flex flex-wrap items-center gap-3 text-xs pb-5 border-b"
                style={{ color: "var(--fg-muted)", borderColor: "var(--border-hairline)" }}
              >
                {activeArticle.author && <span>作者：{activeArticle.author}</span>}
                <span>{new Date(activeArticle.published_at).toLocaleString("zh-CN")}</span>
                {isSafeHttpUrl(activeArticle.canonical_url) &&
                  !activeArticle.canonical_url.includes("inkpress.local") && (
                    <a
                      href={activeArticle.canonical_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 hover:underline"
                      style={{ color: "var(--accent)" }}
                    >
                      <span>原文链接</span>
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
              </div>
            </div>

            {activeArticle.ai_summary && (
              <div
                className="rounded-xl border-l-4 px-5 py-4 mb-8 text-sm leading-relaxed"
                style={{
                  backgroundColor: "var(--bg-subtle)",
                  borderLeftColor: "var(--accent)",
                  color: "var(--fg-secondary)",
                }}
              >
                <div
                  className="text-xs font-semibold tracking-wider uppercase mb-1.5"
                  style={{ color: "var(--accent)" }}
                >
                  AI 导读摘要
                </div>
                <p className="whitespace-pre-line">{activeArticle.ai_summary}</p>
              </div>
            )}

            <div
              className={`editorial-prose ${fontFamily === "serif" ? "font-editorial" : "font-sans-clean"}`}
              style={{ fontSize: `${fontSize}px`, lineHeight }}
              dangerouslySetInnerHTML={{ __html: proxiedHtmlContent }}
            />

            {/* Bottom Article Navigation Bar */}
            <nav
              className="mt-12 pt-6 border-t flex items-center justify-between gap-2"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <button
                type="button"
                disabled={!prevArticle}
                onClick={() => prevArticle && setSelectedId(prevArticle.id)}
                title={prevArticle?.title}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg border text-xs sm:text-sm cursor-pointer disabled:opacity-40"
                style={{
                  borderColor: "var(--border-hairline)",
                  backgroundColor: "var(--bg-surface)",
                  color: "var(--fg-secondary)",
                }}
              >
                <ChevronLeft className="w-4 h-4" />
                <span>上一篇</span>
              </button>

              <button
                type="button"
                onClick={() => setSelectedId(null)}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs sm:text-sm font-medium cursor-pointer"
                style={{
                  backgroundColor: "var(--bg-subtle)",
                  color: "var(--fg-secondary)",
                }}
              >
                <ArrowLeft className="w-3.5 h-3.5" />
                <span>返回今日刊</span>
              </button>

              <button
                type="button"
                disabled={!nextArticle}
                onClick={() => nextArticle && setSelectedId(nextArticle.id)}
                title={nextArticle?.title}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg border text-xs sm:text-sm cursor-pointer disabled:opacity-40"
                style={{
                  borderColor: "var(--border-hairline)",
                  backgroundColor: "var(--bg-surface)",
                  color: "var(--fg-secondary)",
                }}
              >
                <span>下一篇</span>
                <ChevronRight className="w-4 h-4" />
              </button>
            </nav>
          </article>
        )}
      </div>
    );
  }

  // Newspaper Frontpage View
  const leadStory = items[0] ?? null;
  const restStories = items.slice(1);

  return (
    <div className="max-w-5xl mx-auto pb-16">
      {/* Masthead */}
      <header className="pb-6 mb-7 border-b-2" style={{ borderColor: "var(--fg-primary)" }}>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p
              className="text-xs uppercase tracking-[0.2em] mb-1.5 font-medium"
              style={{ color: "var(--accent)" }}
            >
              {todayLabel}
            </p>
            <h1 className="font-editorial text-4xl sm:text-5xl font-semibold tracking-tight">
              今日刊 · Today
            </h1>
          </div>

          <div className="flex items-center gap-2.5 w-full sm:w-auto">
            <div className="relative flex-1 sm:w-64">
              <Search
                className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2"
                style={{ color: "var(--fg-muted)" }}
              />
              <input
                ref={searchRef}
                type="text"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="全文检索（按 / 聚焦）"
                className="w-full rounded-full border pl-9 pr-8 py-1.5 text-sm focus:outline-none"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                }}
              />
              {searchInput && (
                <button
                  type="button"
                  onClick={() => setSearchInput("")}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer"
                  style={{ color: "var(--fg-muted)" }}
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => listQuery.refetch()}
              className="p-2 rounded-full border cursor-pointer hover:opacity-80"
              style={{
                backgroundColor: "var(--bg-surface)",
                borderColor: "var(--border-hairline)",
              }}
              title="刷新列表"
            >
              <RefreshCw className={`w-4 h-4 ${listQuery.isFetching ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>

        {/* Category filter bar */}
        <div className="flex flex-wrap items-center gap-2 mt-5">
          <button
            type="button"
            onClick={() => {
              setActiveCategory("");
              setStarredOnly(false);
              setUnreadOnly(false);
            }}
            className="text-xs px-3 py-1 rounded-full border cursor-pointer"
            style={{
              backgroundColor:
                !activeCategory && !starredOnly && !unreadOnly
                  ? "var(--fg-primary)"
                  : "var(--bg-surface)",
              color:
                !activeCategory && !starredOnly && !unreadOnly
                  ? "var(--bg-canvas)"
                  : "var(--fg-secondary)",
              borderColor: "var(--border-hairline)",
            }}
          >
            全部 ({allArticlesTotal})
          </button>

          <button
            type="button"
            onClick={() => setUnreadOnly((v) => !v)}
            className="inline-flex items-center gap-1 text-xs px-3 py-1 rounded-full border cursor-pointer"
            style={{
              backgroundColor: unreadOnly ? "var(--accent)" : "var(--bg-surface)",
              color: unreadOnly ? "var(--accent-fg)" : "var(--fg-secondary)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <CircleDot className="w-3 h-3" />
            <span>仅看未读</span>
          </button>

          <button
            type="button"
            onClick={() => setStarredOnly((v) => !v)}
            className="inline-flex items-center gap-1 text-xs px-3 py-1 rounded-full border cursor-pointer"
            style={{
              backgroundColor: starredOnly ? "var(--accent)" : "var(--bg-surface)",
              color: starredOnly ? "var(--accent-fg)" : "var(--fg-secondary)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <Bookmark className="w-3 h-3" />
            <span>收藏夹</span>
          </button>

          {categories.map((cat) => (
            <button
              key={cat.name}
              type="button"
              onClick={() => setActiveCategory(activeCategory === cat.name ? "" : cat.name)}
              className="text-xs px-3 py-1 rounded-full border cursor-pointer"
              style={{
                backgroundColor:
                  activeCategory === cat.name ? "var(--fg-primary)" : "var(--bg-surface)",
                color: activeCategory === cat.name ? "var(--bg-canvas)" : "var(--fg-secondary)",
                borderColor: "var(--border-hairline)",
              }}
            >
              {cat.name} · {cat.count}
            </button>
          ))}
        </div>
      </header>

      {listQuery.isError ? (
        <div
          className="rounded-2xl border p-10 text-center space-y-3"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-danger)",
          }}
        >
          <AlertCircle className="w-7 h-7 mx-auto" style={{ color: "var(--fg-danger)" }} />
          <p className="font-editorial text-xl font-semibold">无法加载文章列表</p>
          <p className="text-xs" style={{ color: "var(--fg-danger)" }}>
            {listQuery.error instanceof Error ? listQuery.error.message : "请检查网络或服务状态"}
          </p>
          <button
            type="button"
            onClick={() => listQuery.refetch()}
            className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
          >
            重新加载
          </button>
        </div>
      ) : listQuery.isLoading ? (
        <div className="py-24 text-center text-sm" style={{ color: "var(--fg-muted)" }}>
          正在排版今日内容…
        </div>
      ) : items.length === 0 ? (
        <div
          className="rounded-2xl border p-12 text-center"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-hairline)",
          }}
        >
          <p className="font-editorial text-2xl mb-2">版面尚空</p>
          <p className="text-sm mb-6" style={{ color: "var(--fg-secondary)" }}>
            {unreadOnly || starredOnly || activeCategory || debouncedQuery
              ? "当前筛选条件下暂无匹配文章，可切换筛选或清空搜索词查看全部。"
              : "前往「订阅与剪藏」挑选 NewsNow 实时源、添加 RSS / RSSHub，或粘贴文章链接即可立刻开始阅读。"}
          </p>
          {unreadOnly || starredOnly || activeCategory || debouncedQuery ? (
            <button
              type="button"
              onClick={() => {
                setActiveCategory("");
                setStarredOnly(false);
                setUnreadOnly(false);
                setSearchInput("");
              }}
              className="px-5 py-2.5 rounded-lg text-sm font-medium cursor-pointer"
              style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
            >
              查看全部文章
            </button>
          ) : (
            <button
              type="button"
              onClick={onGoSources}
              className="px-5 py-2.5 rounded-lg text-sm font-medium cursor-pointer"
              style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
            >
              前往订阅源广场
            </button>
          )}
        </div>
      ) : (
        <>
          {/* Lead Frontpage Feature */}
          {leadStory && (
            <section
              onClick={() => setSelectedId(leadStory.id)}
              className="group cursor-pointer rounded-2xl border p-6 sm:p-8 mb-8 grid grid-cols-1 md:grid-cols-12 gap-6 items-center transition-transform"
              style={{
                backgroundColor: "var(--bg-surface)",
                borderColor: "var(--border-hairline)",
                boxShadow: "var(--shadow-paper)",
              }}
            >
              <div className={leadStory.lead_image_url ? "md:col-span-7" : "md:col-span-12"}>
                <div
                  className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider mb-3"
                  style={{ color: "var(--accent)" }}
                >
                  <span>头条聚焦</span>
                  <span>·</span>
                  <span>{leadStory.source_name || leadStory.category}</span>
                  <span style={{ color: "var(--fg-muted)" }}>
                    · {formatRelativeTime(leadStory.published_at)}
                  </span>
                </div>
                <h2 className="font-editorial text-2xl sm:text-3xl font-semibold leading-snug mb-3 group-hover:underline decoration-1 underline-offset-4">
                  {leadStory.translated_title || leadStory.title}
                </h2>
                <p
                  className="text-sm sm:text-base leading-relaxed line-clamp-3 mb-4"
                  style={{ color: "var(--fg-secondary)" }}
                >
                  {leadStory.excerpt}
                </p>
                <div className="flex items-center gap-4 text-xs" style={{ color: "var(--fg-muted)" }}>
                  <span>{leadStory.reading_time_min} 分钟阅读</span>
                  <span>{leadStory.word_count} 字</span>
                  {leadStory.ai_summary && (
                    <span
                      className="px-2 py-0.5 rounded-full"
                      style={{ backgroundColor: "var(--accent-soft)", color: "var(--accent)" }}
                    >
                      含 AI 导读
                    </span>
                  )}
                </div>
              </div>
              {leadStory.lead_image_url && (
                <div className="md:col-span-5">
                  <img
                    src={mediaProxyUrl(leadStory.lead_image_url, leadStory.canonical_url)}
                    alt={leadStory.title}
                    loading="lazy"
                    className="w-full h-56 object-cover rounded-xl border"
                    style={{ borderColor: "var(--border-hairline)" }}
                  />
                </div>
              )}
            </section>
          )}

          {/* Editorial Two-Column Story Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
            {restStories.map((story) => {
              const isPushingThis = pushingArticleId === story.id;
              return (
                <article
                  key={story.id}
                  onClick={() => setSelectedId(story.id)}
                  className="group cursor-pointer pb-6 border-b flex flex-col justify-between"
                  style={{
                    borderColor: "var(--border-hairline)",
                    opacity: story.is_read ? 0.72 : 1,
                  }}
                >
                  <div>
                    <div className="flex items-center justify-between gap-2 mb-2">
                      <div className="flex items-center gap-2 text-xs min-w-0">
                        <span className="font-medium shrink-0" style={{ color: "var(--accent)" }}>
                          {story.category}
                        </span>
                        <span style={{ color: "var(--fg-muted)" }}>·</span>
                        <span className="truncate" style={{ color: "var(--fg-secondary)" }}>
                          {story.source_name}
                        </span>
                        <span className="shrink-0" style={{ color: "var(--fg-muted)" }}>
                          · {formatRelativeTime(story.published_at)}
                        </span>
                      </div>
                      <div
                        className="flex items-center gap-1 shrink-0"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <button
                          type="button"
                          onClick={() => toggleStar(story)}
                          className="p-1 rounded hover:opacity-80 cursor-pointer"
                          style={{
                            color: story.is_starred ? "var(--accent)" : "var(--fg-muted)",
                          }}
                          title="收藏"
                        >
                          <Bookmark className="w-3.5 h-3.5" />
                        </button>
                        <button
                          type="button"
                          disabled={isPushingThis || busyAction !== null}
                          onClick={() => pushToKindle(story.id)}
                          className="p-1 rounded hover:opacity-80 cursor-pointer disabled:opacity-40"
                          style={{ color: isPushingThis ? "var(--accent)" : "var(--fg-muted)" }}
                          title={isPushingThis ? "推送中…" : "推送到 Kindle"}
                        >
                          <Send className={`w-3.5 h-3.5 ${isPushingThis ? "animate-pulse" : ""}`} />
                        </button>
                      </div>
                    </div>

                    <h3 className="font-editorial text-xl font-semibold leading-snug mb-2 group-hover:underline decoration-1 underline-offset-4">
                      {story.translated_title || story.title}
                    </h3>
                    <p
                      className="text-sm leading-relaxed line-clamp-2 mb-3"
                      style={{ color: "var(--fg-secondary)" }}
                    >
                      {story.excerpt}
                    </p>
                  </div>

                  <div className="flex items-center gap-3 text-xs" style={{ color: "var(--fg-muted)" }}>
                    <span>{story.reading_time_min} 分钟</span>
                    <span>·</span>
                    <span>{story.word_count} 字</span>
                    {story.ai_summary && <span style={{ color: "var(--accent)" }}>· AI 导读</span>}
                    {story.has_bilingual && <span style={{ color: "var(--accent)" }}>· 双语</span>}
                  </div>
                </article>
              );
            })}
          </div>

          {/* Pagination: Load More */}
          {listQuery.hasNextPage && (
            <div className="mt-10 text-center space-y-2">
              <button
                type="button"
                disabled={listQuery.isFetchingNextPage}
                onClick={() => listQuery.fetchNextPage()}
                className="px-6 py-2.5 rounded-full border text-sm font-medium cursor-pointer disabled:opacity-50"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                  color: "var(--fg-primary)",
                  boxShadow: "var(--shadow-paper)",
                }}
              >
                {listQuery.isFetchingNextPage ? "正在载入更多…" : "加载更多文章"}
              </button>
              <div className="text-xs tabular-nums" style={{ color: "var(--fg-muted)" }}>
                已载入 {items.length} / 共 {totalCount} 篇
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
