import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bookmark,
  ExternalLink,
  Languages,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  X,
} from "lucide-react";
import { apiRequest, formatRelativeTime } from "../lib/api";
import { useReaderAppearance } from "../lib/theme";
import type { ArticleDetail, ArticleSummary } from "../lib/types";
import { ReaderSettingsBar } from "../components/ReaderSettingsBar";
import { useToast } from "../components/Toast";

interface ArticlesResponse {
  items: ArticleSummary[];
  categories: { name: string; count: number }[];
}

export function TodayPage({ onGoSources }: { onGoSources: () => void }) {
  const qc = useQueryClient();
  const { notify } = useToast();
  const { fontFamily, fontSize, lineHeight } = useReaderAppearance();

  const [searchInput, setSearchInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState<string>("");
  const [starredOnly, setStarredOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showBilingual, setShowBilingual] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(searchInput.trim()), 260);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  const listQuery = useQuery({
    queryKey: ["articles", debouncedQuery, activeCategory, starredOnly],
    queryFn: () => {
      const params = new URLSearchParams();
      if (debouncedQuery) params.set("q", debouncedQuery);
      if (activeCategory) params.set("category", activeCategory);
      if (starredOnly) params.set("starred", "true");
      params.set("limit", "60");
      return apiRequest<ArticlesResponse>(`/api/v1/articles?${params.toString()}`);
    },
  });

  const detailQuery = useQuery({
    queryKey: ["article-detail", selectedId],
    enabled: selectedId !== null,
    queryFn: () => apiRequest<{ item: ArticleDetail }>(`/api/v1/articles/${selectedId}`),
  });

  const items = listQuery.data?.items ?? [];
  const categories = listQuery.data?.categories ?? [];
  const activeArticle = detailQuery.data?.item ?? null;

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
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") {
        if (e.key === "Escape") (e.target as HTMLElement).blur();
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
    setBusyAction("push");
    try {
      await apiRequest(`/api/v1/articles/${articleId}/push`, { method: "POST" });
      notify("已打包为 EPUB 并推送到 Kindle", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "推送失败", "error");
    } finally {
      setBusyAction(null);
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
    const htmlContent =
      showBilingual && activeArticle?.bilingual_html
        ? activeArticle.bilingual_html
        : activeArticle?.clean_html || "";

    return (
      <div className="max-w-3xl mx-auto pb-20">
        <div
          className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-2 py-3 mb-8 border-b backdrop-blur-sm"
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
                >
                  <Bookmark className="w-3.5 h-3.5" />
                </button>

                <button
                  type="button"
                  disabled={busyAction !== null}
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

        {detailQuery.isLoading || !activeArticle ? (
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
                {activeArticle.translated_title || activeArticle.title}
              </h1>
              {activeArticle.translated_title && (
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
                {activeArticle.canonical_url.startsWith("http") &&
                  !activeArticle.canonical_url.includes("inkpress.local") && (
                    <a
                      href={activeArticle.canonical_url}
                      target="_blank"
                      rel="noreferrer"
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
                <p>{activeArticle.ai_summary}</p>
              </div>
            )}

            <div
              className={`editorial-prose ${fontFamily === "serif" ? "font-editorial" : "font-sans-clean"}`}
              style={{ fontSize: `${fontSize}px`, lineHeight }}
              dangerouslySetInnerHTML={{ __html: htmlContent }}
            />
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
      <header
        className="pb-6 mb-7 border-b-2"
        style={{ borderColor: "var(--fg-primary)" }}
      >
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
            }}
            className="text-xs px-3 py-1 rounded-full border cursor-pointer"
            style={{
              backgroundColor:
                !activeCategory && !starredOnly ? "var(--fg-primary)" : "var(--bg-surface)",
              color: !activeCategory && !starredOnly ? "var(--bg-canvas)" : "var(--fg-secondary)",
              borderColor: "var(--border-hairline)",
            }}
          >
            全部 ({items.length})
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

      {listQuery.isLoading ? (
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
            前往「订阅与剪藏」挑选 NewsNow 实时源、添加 RSS / RSSHub，或粘贴文章链接即可立刻开始阅读。
          </p>
          <button
            type="button"
            onClick={onGoSources}
            className="px-5 py-2.5 rounded-lg text-sm font-medium cursor-pointer"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
          >
            前往订阅源广场
          </button>
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
                    src={leadStory.lead_image_url}
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
            {restStories.map((story) => (
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
                    <div className="flex items-center gap-2 text-xs">
                      <span className="font-medium" style={{ color: "var(--accent)" }}>
                        {story.category}
                      </span>
                      <span style={{ color: "var(--fg-muted)" }}>·</span>
                      <span style={{ color: "var(--fg-secondary)" }}>{story.source_name}</span>
                      <span style={{ color: "var(--fg-muted)" }}>
                        · {formatRelativeTime(story.published_at)}
                      </span>
                    </div>
                    <div
                      className="flex items-center gap-1"
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
                        onClick={() => pushToKindle(story.id)}
                        className="p-1 rounded hover:opacity-80 cursor-pointer"
                        style={{ color: "var(--fg-muted)" }}
                        title="推送到 Kindle"
                      >
                        <Send className="w-3.5 h-3.5" />
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
            ))}
          </div>
        </>
      )}
    </div>
  );
}
