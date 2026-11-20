import React, { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Download,
  Eye,
  Flame,
  Link2,
  Pencil,
  Plus,
  RefreshCw,
  Rss,
  Scissors,
  Send,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { apiRequest, formatRelativeTime, isSafeHttpUrl } from "../lib/api";
import type {
  FetchStrategy,
  NewsNowCatalogItem,
  ProxyMode,
  RssPreset,
  SourceAiOptions,
  SourceItem,
  SourceKind,
} from "../lib/types";
import { useToast } from "../components/Toast";

type SourcesTab = "newsnow" | "subscriptions" | "clip";
type CustomSourceKind = Exclude<SourceKind, "newsnow">;

function parseRemoveSelectors(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function buildSourceAiOptions(
  base: SourceAiOptions | undefined,
  fields: {
    titleInclude: string;
    titleExclude: string;
    linkSelector: string;
    contentSelector: string;
    removeSelectors: string;
  },
): SourceAiOptions {
  const next: SourceAiOptions = {
    summarize: Boolean(base?.summarize),
    bilingual: Boolean(base?.bilingual),
  };
  const inc = fields.titleInclude.trim();
  const exc = fields.titleExclude.trim();
  const linkSel = fields.linkSelector.trim();
  const contentSel = fields.contentSelector.trim();
  const removeList = parseRemoveSelectors(fields.removeSelectors);

  if (inc) next.title_include_regex = inc;
  if (exc) next.title_exclude_regex = exc;
  if (linkSel) next.link_selector = linkSel;
  if (contentSel) next.content_selector = contentSel;
  if (removeList.length > 0) next.remove_selectors = removeList;
  return next;
}

export function SourcesPage() {
  const qc = useQueryClient();
  const { notify } = useToast();

  const [tab, setTab] = useState<SourcesTab>("newsnow");
  const [nnCategory, setNnCategory] = useState("");
  const [previewSource, setPreviewSource] = useState<{
    id: string;
    name: string;
    items: { title: string; url: string; summary_hint: string; info_tag: string }[];
  } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Add custom RSS / RSSHub / webpage / webpage_list form
  const [rssName, setRssName] = useState("");
  const [rssKind, setRssKind] = useState<CustomSourceKind>("rss");
  const [rssIdentifier, setRssIdentifier] = useState("");
  const [rssCategory, setRssCategory] = useState("综合");
  const [rssStrategy, setRssStrategy] = useState<"fulltext_rss" | "extract_web" | "byparr_browser">(
    "extract_web",
  );
  const [rssProxy, setRssProxy] = useState<ProxyMode>("auto");
  const [showAdvancedAdd, setShowAdvancedAdd] = useState(false);
  const [rssTitleInclude, setRssTitleInclude] = useState("");
  const [rssTitleExclude, setRssTitleExclude] = useState("");
  const [rssLinkSelector, setRssLinkSelector] = useState("");
  const [rssContentSelector, setRssContentSelector] = useState("");
  const [rssRemoveSelectors, setRssRemoveSelectors] = useState("");

  // Edit source modal state
  const [editingSource, setEditingSource] = useState<SourceItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editIdentifier, setEditIdentifier] = useState("");
  const [editCategory, setEditCategory] = useState("");
  const [editStrategy, setEditStrategy] = useState<FetchStrategy>("extract_web");
  const [editProxy, setEditProxy] = useState<ProxyMode>("auto");
  const [editMaxItems, setEditMaxItems] = useState(15);
  const [editEnabled, setEditEnabled] = useState(true);
  const [editTitleInclude, setEditTitleInclude] = useState("");
  const [editTitleExclude, setEditTitleExclude] = useState("");
  const [editLinkSelector, setEditLinkSelector] = useState("");
  const [editContentSelector, setEditContentSelector] = useState("");
  const [editRemoveSelectors, setEditRemoveSelectors] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // Clip & Push form
  const [clipUrl, setClipUrl] = useState("");
  const [clipPush, setClipPush] = useState(true);
  const [clipBusy, setClipBusy] = useState(false);
  const [textTitle, setTextTitle] = useState("");
  const [textBody, setTextBody] = useState("");
  const [textPush, setTextPush] = useState(true);

  const catalogQuery = useQuery({
    queryKey: ["newsnow-catalog"],
    queryFn: () =>
      apiRequest<{ items: NewsNowCatalogItem[]; presets: RssPreset[] }>(
        "/api/v1/sources/catalog/newsnow",
      ),
  });

  const sourcesQuery = useQuery({
    queryKey: ["sources"],
    queryFn: () => apiRequest<{ items: SourceItem[] }>("/api/v1/sources"),
  });

  const catalog = catalogQuery.data?.items ?? [];
  const presets = catalogQuery.data?.presets ?? [];
  const sources = sourcesQuery.data?.items ?? [];

  const nnCategories = Array.from(new Set(catalog.map((c) => c.category)));
  const filteredCatalog = nnCategory ? catalog.filter((c) => c.category === nnCategory) : catalog;

  const openEditModal = (src: SourceItem) => {
    setEditingSource(src);
    setEditName(src.name);
    setEditIdentifier(src.identifier);
    setEditCategory(src.category);
    setEditStrategy(src.fetch_strategy);
    setEditProxy(src.use_proxy);
    setEditMaxItems(src.max_items);
    setEditEnabled(src.enabled);
    setEditTitleInclude(src.ai_options?.title_include_regex ?? "");
    setEditTitleExclude(src.ai_options?.title_exclude_regex ?? "");
    setEditLinkSelector(src.ai_options?.link_selector ?? "");
    setEditContentSelector(src.ai_options?.content_selector ?? "");
    setEditRemoveSelectors((src.ai_options?.remove_selectors ?? []).join(", "));
  };

  const saveEditedSource = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingSource) return;
    setEditSaving(true);
    try {
      const nextAiOptions = buildSourceAiOptions(editingSource.ai_options, {
        titleInclude: editTitleInclude,
        titleExclude: editTitleExclude,
        linkSelector: editLinkSelector,
        contentSelector: editContentSelector,
        removeSelectors: editRemoveSelectors,
      });
      await apiRequest(`/api/v1/sources/${editingSource.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editName.trim(),
          identifier: editIdentifier.trim(),
          category: editCategory.trim() || "综合",
          fetch_strategy: editStrategy,
          use_proxy: editProxy,
          max_items: Math.min(60, Math.max(1, editMaxItems)),
          enabled: editEnabled,
          ai_options: nextAiOptions,
        }),
      });
      setEditingSource(null);
      await qc.invalidateQueries({ queryKey: ["sources"] });
      await qc.invalidateQueries({ queryKey: ["newsnow-catalog"] });
      notify("订阅源配置已更新", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setEditSaving(false);
    }
  };

  const subscribeNewsNow = async (item: NewsNowCatalogItem) => {
    try {
      await apiRequest("/api/v1/sources", {
        method: "POST",
        body: JSON.stringify({
          name: item.name,
          kind: "newsnow",
          identifier: item.id,
          category: item.category,
          fetch_strategy: item.default_strategy,
          use_proxy: "auto",
          ai_options: { summarize: false, bilingual: item.language === "en" },
          max_items: 15,
        }),
      });
      await qc.invalidateQueries({ queryKey: ["newsnow-catalog"] });
      await qc.invalidateQueries({ queryKey: ["sources"] });
      notify(`已订阅 NewsNow 源：${item.name}`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "订阅失败", "error");
    }
  };

  const openPreview = async (item: NewsNowCatalogItem) => {
    setPreviewLoading(true);
    try {
      const res = await apiRequest<{
        items: { title: string; url: string; summary_hint: string; info_tag: string }[];
      }>(`/api/v1/sources/catalog/newsnow/${item.id}/preview`);
      setPreviewSource({ id: item.id, name: item.name, items: res.items });
    } catch (err) {
      notify(err instanceof Error ? err.message : "预览失败，请检查 NewsNow Sidecar 状态", "error");
    } finally {
      setPreviewLoading(false);
    }
  };

  const addCustomSource = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const aiOptions = buildSourceAiOptions(undefined, {
        titleInclude: rssTitleInclude,
        titleExclude: rssTitleExclude,
        linkSelector: rssKind === "webpage_list" ? rssLinkSelector : "",
        contentSelector: rssContentSelector,
        removeSelectors: rssRemoveSelectors,
      });
      await apiRequest("/api/v1/sources", {
        method: "POST",
        body: JSON.stringify({
          name: rssName.trim(),
          kind: rssKind,
          identifier: rssIdentifier.trim(),
          category: rssCategory.trim() || "综合",
          fetch_strategy: rssStrategy,
          use_proxy: rssProxy,
          ai_options: aiOptions,
        }),
      });
      setRssName("");
      setRssIdentifier("");
      setRssTitleInclude("");
      setRssTitleExclude("");
      setRssLinkSelector("");
      setRssContentSelector("");
      setRssRemoveSelectors("");
      await qc.invalidateQueries({ queryKey: ["sources"] });
      notify("已添加订阅源并开始后台抓取", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "添加失败", "error");
    }
  };

  const addPreset = async (p: RssPreset) => {
    try {
      await apiRequest("/api/v1/sources", {
        method: "POST",
        body: JSON.stringify({
          name: p.name,
          kind: p.kind,
          identifier: p.identifier,
          category: p.category,
          fetch_strategy: p.fetch_strategy,
          use_proxy: p.use_proxy,
          ...(p.ai_options ? { ai_options: p.ai_options } : {}),
        }),
      });
      await qc.invalidateQueries({ queryKey: ["sources"] });
      notify(`已添加精选源：${p.name}`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "添加失败", "error");
    }
  };

  const syncSource = async (src: SourceItem) => {
    try {
      await apiRequest(`/api/v1/sources/${src.id}/sync`, { method: "POST" });
      notify(`已触发同步：${src.name}`, "success");
      window.setTimeout(() => qc.invalidateQueries({ queryKey: ["sources"] }), 2500);
    } catch (err) {
      notify(err instanceof Error ? err.message : "同步失败", "error");
    }
  };

  const toggleSourceAi = async (src: SourceItem, key: "summarize" | "bilingual") => {
    try {
      const next = { ...(src.ai_options || {}), [key]: !src.ai_options?.[key] };
      await apiRequest(`/api/v1/sources/${src.id}`, {
        method: "PATCH",
        body: JSON.stringify({ ai_options: next }),
      });
      await qc.invalidateQueries({ queryKey: ["sources"] });
    } catch (err) {
      notify(err instanceof Error ? err.message : "更新失败", "error");
    }
  };

  const deleteSource = async (src: SourceItem) => {
    if (!window.confirm(`确认取消订阅「${src.name}」？历史文章与收藏将被保留。`)) return;
    try {
      await apiRequest(`/api/v1/sources/${src.id}`, { method: "DELETE" });
      await qc.invalidateQueries({ queryKey: ["sources"] });
      await qc.invalidateQueries({ queryKey: ["newsnow-catalog"] });
      notify("已移除订阅源", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "移除失败", "error");
    }
  };

  const importOpml = async (files: FileList | null) => {
    if (!files || !files[0]) return;
    const fd = new FormData();
    fd.append("file", files[0]);
    try {
      const res = await apiRequest<{ added: number; skipped: number }>(
        "/api/v1/sources/import-opml",
        { method: "POST", body: fd },
      );
      await qc.invalidateQueries({ queryKey: ["sources"] });
      notify(`OPML 导入完成：新增 ${res.added} 个，跳过 ${res.skipped} 个`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "导入失败", "error");
    }
  };

  const submitClipUrl = async (e: React.FormEvent) => {
    e.preventDefault();
    setClipBusy(true);
    try {
      await apiRequest("/api/v1/articles/clip-url", {
        method: "POST",
        body: JSON.stringify({ url: clipUrl.trim(), push_to_kindle: clipPush }),
      });
      setClipUrl("");
      await qc.invalidateQueries({ queryKey: ["articles"] });
      notify(clipPush ? "网页已剪藏并推送到 Kindle" : "网页已剪藏到今日刊收藏夹", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "剪藏失败", "error");
    } finally {
      setClipBusy(false);
    }
  };

  const submitClipText = async (e: React.FormEvent) => {
    e.preventDefault();
    setClipBusy(true);
    try {
      await apiRequest("/api/v1/articles/clip-text", {
        method: "POST",
        body: JSON.stringify({
          title: textTitle.trim(),
          content: textBody,
          content_format: "markdown",
          push_to_kindle: textPush,
        }),
      });
      setTextTitle("");
      setTextBody("");
      await qc.invalidateQueries({ queryKey: ["articles"] });
      notify(textPush ? "文稿已转为 EPUB 并推送到 Kindle" : "文稿已收藏", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "提交失败", "error");
    } finally {
      setClipBusy(false);
    }
  };

  const identifierPlaceholder =
    rssKind === "rsshub"
      ? "rsshub://cw/today"
      : rssKind === "webpage_list"
        ? "https://www.cw.com.tw/today"
        : rssKind === "webpage"
          ? "https://example.com/article"
          : "https://example.com/feed.xml";

  const advancedPanelOpen = showAdvancedAdd || rssKind === "webpage_list";

  return (
    <div className="max-w-5xl mx-auto pb-16">
      <header className="pb-6 mb-7 border-b-2" style={{ borderColor: "var(--fg-primary)" }}>
        <p className="text-xs uppercase tracking-[0.2em] mb-1.5" style={{ color: "var(--accent)" }}>
          CURATION &amp; CLIPPING
        </p>
        <h1 className="font-editorial text-4xl font-semibold">订阅与剪藏 · Sources</h1>
        <div className="flex flex-wrap gap-2 mt-5">
          {[
            { id: "newsnow" as const, label: `NewsNow 源广场 (${catalog.length})`, icon: Flame },
            { id: "subscriptions" as const, label: `我的订阅 & RSSHub (${sources.length})`, icon: Rss },
            { id: "clip" as const, label: "网页 / 文本剪藏推送", icon: Scissors },
          ].map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className="inline-flex items-center gap-1.5 text-xs font-medium px-4 py-2 rounded-full border cursor-pointer"
                style={{
                  backgroundColor: active ? "var(--fg-primary)" : "var(--bg-surface)",
                  color: active ? "var(--bg-canvas)" : "var(--fg-secondary)",
                  borderColor: "var(--border-hairline)",
                }}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </header>

      {tab === "newsnow" && (
        <div>
          <div className="flex flex-wrap gap-2 mb-5">
            <button
              type="button"
              onClick={() => setNnCategory("")}
              className="text-xs px-3 py-1 rounded-full border cursor-pointer"
              style={{
                backgroundColor: !nnCategory ? "var(--accent)" : "var(--bg-surface)",
                color: !nnCategory ? "var(--accent-fg)" : "var(--fg-secondary)",
                borderColor: "var(--border-hairline)",
              }}
            >
              全部分类
            </button>
            {nnCategories.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setNnCategory(c)}
                className="text-xs px-3 py-1 rounded-full border cursor-pointer"
                style={{
                  backgroundColor: nnCategory === c ? "var(--accent)" : "var(--bg-surface)",
                  color: nnCategory === c ? "var(--accent-fg)" : "var(--fg-secondary)",
                  borderColor: "var(--border-hairline)",
                }}
              >
                {c}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {filteredCatalog.map((item) => (
              <div
                key={item.id}
                className="rounded-xl border p-4 flex flex-col justify-between gap-3"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                }}
              >
                <div>
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span className="font-editorial text-lg font-semibold">{item.name}</span>
                    <span
                      className="text-[11px] px-2 py-0.5 rounded-full shrink-0"
                      style={{
                        backgroundColor: "var(--bg-subtle)",
                        color: "var(--fg-secondary)",
                      }}
                    >
                      {item.category} · {item.default_strategy === "extract_web" ? "全文抽取" : "热点快讯"}
                    </span>
                  </div>
                  <p className="text-xs leading-relaxed" style={{ color: "var(--fg-secondary)" }}>
                    {item.description}
                  </p>
                </div>

                <div
                  className="flex items-center justify-between pt-2 border-t"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <button
                    type="button"
                    disabled={previewLoading}
                    onClick={() => openPreview(item)}
                    className="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer"
                    style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
                  >
                    <Eye className="w-3.5 h-3.5" />
                    <span>实时预览</span>
                  </button>
                  {item.subscribed ? (
                    <span
                      className="inline-flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-lg"
                      style={{ backgroundColor: "var(--accent-soft)", color: "var(--accent)" }}
                    >
                      <Check className="w-3.5 h-3.5" />
                      <span>已订阅</span>
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => subscribeNewsNow(item)}
                      className="inline-flex items-center gap-1 text-xs font-medium px-3 py-1.5 rounded-lg cursor-pointer"
                      style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                    >
                      <Plus className="w-3.5 h-3.5" />
                      <span>订阅</span>
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "subscriptions" && (
        <div className="space-y-8">
          {/* Add custom RSS / RSSHub / Webpage / Webpage List */}
          <form
            onSubmit={addCustomSource}
            className="rounded-2xl border p-6 space-y-4"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-editorial text-xl font-semibold">
                添加 RSS / RSSHub / 网页列表源
              </h2>
              <div className="flex items-center gap-2">
                <label
                  className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg border cursor-pointer"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <Upload className="w-3.5 h-3.5" />
                  <span>导入 OPML</span>
                  <input
                    type="file"
                    accept=".opml,.xml"
                    className="hidden"
                    onChange={(e) => {
                      importOpml(e.target.files);
                      e.target.value = "";
                    }}
                  />
                </label>
                <a
                  href="/api/v1/sources/export-opml"
                  className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg border"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>导出 OPML</span>
                </a>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-12 gap-3">
              <input
                type="text"
                required
                placeholder="源名称（如：喷嚏图卦 / 天下杂志）"
                value={rssName}
                onChange={(e) => setRssName(e.target.value)}
                className="sm:col-span-3 rounded-lg border px-3 py-2 text-sm"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <select
                value={rssKind}
                onChange={(e) => {
                  const nextKind = e.target.value as CustomSourceKind;
                  setRssKind(nextKind);
                  if (nextKind === "webpage_list") {
                    setShowAdvancedAdd(true);
                  }
                }}
                className="sm:col-span-3 rounded-lg border px-2.5 py-2 text-sm"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              >
                <option value="rss">RSS / Atom</option>
                <option value="rsshub">RSSHub 路由</option>
                <option value="webpage">单个网页</option>
                <option value="webpage_list">网页列表（按选择器抓链接）</option>
              </select>
              <input
                type="text"
                required
                placeholder={identifierPlaceholder}
                value={rssIdentifier}
                onChange={(e) => setRssIdentifier(e.target.value)}
                className="sm:col-span-6 rounded-lg border px-3 py-2 text-sm"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-12 gap-3 items-center">
              <input
                type="text"
                placeholder="分类（如 科技/财经）"
                value={rssCategory}
                onChange={(e) => setRssCategory(e.target.value)}
                className="sm:col-span-3 rounded-lg border px-3 py-2 text-xs"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              />
              <select
                value={rssStrategy}
                onChange={(e) =>
                  setRssStrategy(e.target.value as "fulltext_rss" | "extract_web" | "byparr_browser")
                }
                className="sm:col-span-4 rounded-lg border px-2.5 py-2 text-xs"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              >
                <option value="fulltext_rss">全文 RSS（直接采用正文）</option>
                <option value="extract_web">自动抽取原网页正文</option>
                <option value="byparr_browser">无头浏览器过反爬（Byparr）</option>
              </select>
              <select
                value={rssProxy}
                onChange={(e) => setRssProxy(e.target.value as ProxyMode)}
                className="sm:col-span-3 rounded-lg border px-2.5 py-2 text-xs"
                style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
              >
                <option value="auto">智能分流（国内外自动）</option>
                <option value="direct">住宅 IP 直连</option>
                <option value="proxy">强制走海外代理</option>
              </select>
              <button
                type="submit"
                className="col-span-2 sm:col-span-2 rounded-lg py-2 text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                添加订阅
              </button>
            </div>

            {/* Optional regex filters & CSS selectors */}
            <div className="pt-1">
              <button
                type="button"
                onClick={() => setShowAdvancedAdd((v) => !v)}
                className="inline-flex items-center gap-1 text-xs cursor-pointer"
                style={{ color: "var(--fg-secondary)" }}
              >
                {advancedPanelOpen ? (
                  <ChevronUp className="w-3.5 h-3.5" />
                ) : (
                  <ChevronDown className="w-3.5 h-3.5" />
                )}
                <span>高级规则：标题正则过滤 / CSS 选择器（可选）</span>
              </button>

              {advancedPanelOpen && (
                <div
                  className="mt-3 rounded-xl border p-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs"
                  style={{
                    backgroundColor: "var(--bg-canvas)",
                    borderColor: "var(--border-hairline)",
                  }}
                >
                  <div>
                    <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                      标题包含正则（仅保留匹配文章）
                    </label>
                    <input
                      type="text"
                      value={rssTitleInclude}
                      onChange={(e) => setRssTitleInclude(e.target.value)}
                      placeholder="例如：喷嚏图卦"
                      className="w-full rounded-lg border px-3 py-1.5 font-mono"
                      style={{
                        backgroundColor: "var(--bg-surface)",
                        borderColor: "var(--border-hairline)",
                      }}
                    />
                  </div>
                  <div>
                    <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                      标题排除正则（跳过匹配文章）
                    </label>
                    <input
                      type="text"
                      value={rssTitleExclude}
                      onChange={(e) => setRssTitleExclude(e.target.value)}
                      placeholder="例如：广告|推广|音频"
                      className="w-full rounded-lg border px-3 py-1.5 font-mono"
                      style={{
                        backgroundColor: "var(--bg-surface)",
                        borderColor: "var(--border-hairline)",
                      }}
                    />
                  </div>
                  {rssKind === "webpage_list" && (
                    <div className="sm:col-span-2">
                      <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                        列表链接 CSS 选择器（抓取列表页中的文章链接）
                      </label>
                      <input
                        type="text"
                        value={rssLinkSelector}
                        onChange={(e) => setRssLinkSelector(e.target.value)}
                        placeholder="例如：a[href*='/article/']"
                        className="w-full rounded-lg border px-3 py-1.5 font-mono"
                        style={{
                          backgroundColor: "var(--bg-surface)",
                          borderColor: "var(--border-hairline)",
                        }}
                      />
                    </div>
                  )}
                  <div>
                    <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                      正文容器 CSS 选择器（可选）
                    </label>
                    <input
                      type="text"
                      value={rssContentSelector}
                      onChange={(e) => setRssContentSelector(e.target.value)}
                      placeholder="例如：article.post-content"
                      className="w-full rounded-lg border px-3 py-1.5 font-mono"
                      style={{
                        backgroundColor: "var(--bg-surface)",
                        borderColor: "var(--border-hairline)",
                      }}
                    />
                  </div>
                  <div>
                    <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                      剔除元素 CSS 选择器（英文逗号分隔）
                    </label>
                    <input
                      type="text"
                      value={rssRemoveSelectors}
                      onChange={(e) => setRssRemoveSelectors(e.target.value)}
                      placeholder="例如：.ads, .related-posts, footer"
                      className="w-full rounded-lg border px-3 py-1.5 font-mono"
                      style={{
                        backgroundColor: "var(--bg-surface)",
                        borderColor: "var(--border-hairline)",
                      }}
                    />
                  </div>
                </div>
              )}
            </div>
          </form>

          {/* Curated presets */}
          <div>
            <h3
              className="text-xs font-semibold uppercase tracking-wider mb-3"
              style={{ color: "var(--fg-muted)" }}
            >
              精选一键订阅模板（含 RSSHub 与网页列表源）
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {presets.map((p) => (
                <div
                  key={`${p.kind}-${p.identifier}`}
                  className="rounded-xl border p-3.5 flex items-center justify-between gap-3"
                  style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">{p.name}</div>
                    <div className="text-xs truncate" style={{ color: "var(--fg-muted)" }}>
                      {p.description}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => addPreset(p)}
                    className="shrink-0 text-xs px-2.5 py-1 rounded-lg border cursor-pointer"
                    style={{ borderColor: "var(--border-hairline)", color: "var(--accent)" }}
                  >
                    + 订阅
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Active subscriptions table */}
          <div className="space-y-3">
            <h3
              className="text-xs font-semibold uppercase tracking-wider"
              style={{ color: "var(--fg-muted)" }}
            >
              已订阅源（{sources.length}）
            </h3>
            {sources.map((src) => {
              const hasRegex = Boolean(
                src.ai_options?.title_include_regex || src.ai_options?.title_exclude_regex,
              );
              const hasSelectors = Boolean(
                src.ai_options?.link_selector ||
                  src.ai_options?.content_selector ||
                  (src.ai_options?.remove_selectors?.length ?? 0) > 0,
              );
              return (
                <div
                  key={src.id}
                  className="rounded-xl border p-4 flex flex-wrap items-center justify-between gap-3"
                  style={{
                    backgroundColor: "var(--bg-surface)",
                    borderColor: "var(--border-hairline)",
                    opacity: src.enabled ? 1 : 0.68,
                  }}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-editorial text-lg font-semibold">{src.name}</span>
                      <span
                        className="text-[11px] px-2 py-0.5 rounded-full"
                        style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
                      >
                        {src.kind.toUpperCase()} · {src.category}
                      </span>
                      {!src.enabled && (
                        <span
                          className="text-[11px] px-2 py-0.5 rounded-full border"
                          style={{
                            borderColor: "var(--border-hairline)",
                            color: "var(--fg-muted)",
                          }}
                        >
                          已停用
                        </span>
                      )}
                      {hasRegex && (
                        <span
                          className="text-[11px] px-2 py-0.5 rounded-full"
                          style={{ backgroundColor: "var(--accent-soft)", color: "var(--accent)" }}
                        >
                          正则过滤
                        </span>
                      )}
                      {hasSelectors && (
                        <span
                          className="text-[11px] px-2 py-0.5 rounded-full"
                          style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
                        >
                          CSS 选择器
                        </span>
                      )}
                      <span className="text-xs" style={{ color: "var(--fg-muted)" }}>
                        已收 {src.article_count} 篇 · 上次同步 {formatRelativeTime(src.last_fetched_at)}
                      </span>
                    </div>
                    <div className="text-xs truncate mt-1" style={{ color: "var(--fg-muted)" }}>
                      {src.identifier}
                    </div>
                    {src.last_error && (
                      <div
                        className="text-xs mt-1 line-clamp-1"
                        style={{ color: "var(--fg-danger)" }}
                      >
                        最近错误：{src.last_error}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 text-xs">
                    <button
                      type="button"
                      onClick={() => toggleSourceAi(src, "summarize")}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border cursor-pointer"
                      style={{
                        borderColor: "var(--border-hairline)",
                        backgroundColor: src.ai_options?.summarize
                          ? "var(--accent-soft)"
                          : "transparent",
                        color: src.ai_options?.summarize ? "var(--accent)" : "var(--fg-muted)",
                      }}
                    >
                      <Sparkles className="w-3 h-3" />
                      <span>AI摘要</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => toggleSourceAi(src, "bilingual")}
                      className="px-2.5 py-1 rounded-lg border cursor-pointer"
                      style={{
                        borderColor: "var(--border-hairline)",
                        backgroundColor: src.ai_options?.bilingual
                          ? "var(--accent-soft)"
                          : "transparent",
                        color: src.ai_options?.bilingual ? "var(--accent)" : "var(--fg-muted)",
                      }}
                    >
                      双语
                    </button>
                    <button
                      type="button"
                      onClick={() => openEditModal(src)}
                      className="p-1.5 rounded-lg border cursor-pointer"
                      style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
                      title="编辑订阅源"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => syncSource(src)}
                      className="p-1.5 rounded-lg border cursor-pointer"
                      style={{ borderColor: "var(--border-hairline)" }}
                      title="立即同步"
                    >
                      <RefreshCw className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteSource(src)}
                      className="p-1.5 rounded-lg border cursor-pointer"
                      style={{ borderColor: "var(--border-hairline)", color: "var(--fg-muted)" }}
                      title="移除"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {tab === "clip" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <form
            onSubmit={submitClipUrl}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <Link2 className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">粘贴网址一键剪藏</h2>
            </div>
            <p className="text-xs leading-relaxed" style={{ color: "var(--fg-secondary)" }}>
              自动智能提取正文与配图（支持 Byparr 绕过反爬），生成干净 EPUB 并推送到你的 Kindle。
            </p>
            <input
              type="url"
              required
              placeholder="https://..."
              value={clipUrl}
              onChange={(e) => setClipUrl(e.target.value)}
              className="w-full rounded-lg border px-3.5 py-2.5 text-sm"
              style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
            />
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={clipPush}
                onChange={(e) => setClipPush(e.target.checked)}
              />
              <span>剪藏完成后立即推送至 Kindle</span>
            </label>
            <button
              type="submit"
              disabled={clipBusy}
              className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-medium cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
            >
              <Send className="w-3.5 h-3.5" />
              <span>{clipBusy ? "处理中…" : clipPush ? "剪藏并推送" : "仅剪藏收藏"}</span>
            </button>
          </form>

          <form
            onSubmit={submitClipText}
            className="rounded-2xl border p-6 space-y-4"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div className="flex items-center gap-2">
              <Scissors className="w-5 h-5" style={{ color: "var(--accent)" }} />
              <h2 className="font-editorial text-xl font-semibold">长文 / Markdown 直推 Kindle</h2>
            </div>
            <input
              type="text"
              required
              placeholder="文章标题"
              value={textTitle}
              onChange={(e) => setTextTitle(e.target.value)}
              className="w-full rounded-lg border px-3.5 py-2 text-sm"
              style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
            />
            <textarea
              required
              rows={6}
              placeholder="粘贴任意 Markdown 或纯文本内容…"
              value={textBody}
              onChange={(e) => setTextBody(e.target.value)}
              className="w-full rounded-lg border px-3.5 py-2.5 text-sm"
              style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-hairline)" }}
            />
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={textPush}
                onChange={(e) => setTextPush(e.target.checked)}
              />
              <span>生成标准 EPUB 后立即推送至 Kindle</span>
            </label>
            <button
              type="submit"
              disabled={clipBusy}
              className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-medium cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
            >
              <Send className="w-3.5 h-3.5" />
              <span>{clipBusy ? "处理中…" : textPush ? "排版并推送" : "保存到收藏"}</span>
            </button>
          </form>
        </div>
      )}

      {/* Edit Source Modal */}
      {editingSource && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4">
          <form
            onSubmit={saveEditedSource}
            className="max-w-xl w-full max-h-[88vh] flex flex-col rounded-2xl border overflow-hidden"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div
              className="px-6 py-4 border-b flex items-center justify-between"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <div>
                <h3 className="font-editorial text-xl font-semibold">编辑订阅源</h3>
                <p className="text-xs mt-0.5" style={{ color: "var(--fg-muted)" }}>
                  类型：{editingSource.kind.toUpperCase()}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setEditingSource(null)}
                className="p-1 cursor-pointer"
                style={{ color: "var(--fg-muted)" }}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-6 overflow-y-auto space-y-4 text-xs">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    源名称
                  </label>
                  <input
                    type="text"
                    required
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    分类
                  </label>
                  <input
                    type="text"
                    value={editCategory}
                    onChange={(e) => setEditCategory(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <div>
                <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                  源地址 / 标识符
                </label>
                <input
                  type="text"
                  required
                  value={editIdentifier}
                  onChange={(e) => setEditIdentifier(e.target.value)}
                  className="w-full rounded-lg border px-3 py-2 text-sm font-mono"
                  style={{
                    backgroundColor: "var(--bg-canvas)",
                    borderColor: "var(--border-hairline)",
                  }}
                />
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    抓取策略
                  </label>
                  <select
                    value={editStrategy}
                    onChange={(e) => setEditStrategy(e.target.value as FetchStrategy)}
                    className="w-full rounded-lg border px-2.5 py-2"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    <option value="fulltext_rss">全文 RSS</option>
                    <option value="extract_web">自动抽取网页正文</option>
                    <option value="newsnow_digest">热点快讯汇总</option>
                    <option value="byparr_browser">Byparr 浏览器过反爬</option>
                  </select>
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    网络路由
                  </label>
                  <select
                    value={editProxy}
                    onChange={(e) => setEditProxy(e.target.value as ProxyMode)}
                    className="w-full rounded-lg border px-2.5 py-2"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    <option value="auto">智能分流</option>
                    <option value="direct">直连</option>
                    <option value="proxy">海外代理</option>
                  </select>
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    单次最大条数
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={60}
                    value={editMaxItems}
                    onChange={(e) => setEditMaxItems(Number(e.target.value))}
                    className="w-full rounded-lg border px-3 py-2"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    标题包含正则（仅保留匹配）
                  </label>
                  <input
                    type="text"
                    value={editTitleInclude}
                    onChange={(e) => setEditTitleInclude(e.target.value)}
                    placeholder="留空不限制"
                    className="w-full rounded-lg border px-3 py-2 font-mono"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    标题排除正则（跳过匹配）
                  </label>
                  <input
                    type="text"
                    value={editTitleExclude}
                    onChange={(e) => setEditTitleExclude(e.target.value)}
                    placeholder="留空不限制"
                    className="w-full rounded-lg border px-3 py-2 font-mono"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              {(editingSource.kind === "webpage_list" || editLinkSelector) && (
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    列表链接 CSS 选择器
                  </label>
                  <input
                    type="text"
                    value={editLinkSelector}
                    onChange={(e) => setEditLinkSelector(e.target.value)}
                    placeholder="例如：a[href*='/article/']"
                    className="w-full rounded-lg border px-3 py-2 font-mono"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              )}

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    正文容器 CSS 选择器
                  </label>
                  <input
                    type="text"
                    value={editContentSelector}
                    onChange={(e) => setEditContentSelector(e.target.value)}
                    placeholder="可选"
                    className="w-full rounded-lg border px-3 py-2 font-mono"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
                <div>
                  <label className="block mb-1" style={{ color: "var(--fg-secondary)" }}>
                    剔除元素 CSS 选择器（逗号分隔）
                  </label>
                  <input
                    type="text"
                    value={editRemoveSelectors}
                    onChange={(e) => setEditRemoveSelectors(e.target.value)}
                    placeholder="例如：.ads, .share"
                    className="w-full rounded-lg border px-3 py-2 font-mono"
                    style={{
                      backgroundColor: "var(--bg-canvas)",
                      borderColor: "var(--border-hairline)",
                    }}
                  />
                </div>
              </div>

              <label className="flex items-center gap-2 pt-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={editEnabled}
                  onChange={(e) => setEditEnabled(e.target.checked)}
                />
                <span>启用定时同步与刊物收录</span>
              </label>
            </div>

            <div
              className="px-6 py-4 border-t flex justify-end gap-2"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <button
                type="button"
                onClick={() => setEditingSource(null)}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                取消
              </button>
              <button
                type="submit"
                disabled={editSaving}
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-50"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                {editSaving ? "保存中…" : "保存修改"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* NewsNow Live Preview Modal */}
      {previewSource && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4">
          <div
            className="max-w-xl w-full max-h-[80vh] flex flex-col rounded-2xl border overflow-hidden"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border-hairline)" }}
          >
            <div
              className="px-5 py-4 border-b flex items-center justify-between"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              <h3 className="font-editorial text-xl font-semibold">
                {previewSource.name} · 实时热榜预览
              </h3>
              <button
                type="button"
                onClick={() => setPreviewSource(null)}
                className="p-1 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div
              className="p-5 overflow-y-auto divide-y"
              style={{ borderColor: "var(--border-hairline)" }}
            >
              {previewSource.items.length === 0 ? (
                <p className="text-xs py-6 text-center" style={{ color: "var(--fg-muted)" }}>
                  暂无实时条目返回
                </p>
              ) : (
                previewSource.items.map((entry, idx) => (
                  <div key={idx} className="py-2.5">
                    {isSafeHttpUrl(entry.url) ? (
                      <a
                        href={entry.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sm font-medium hover:underline"
                      >
                        {idx + 1}. {entry.title}
                      </a>
                    ) : (
                      <span className="text-sm font-medium">
                        {idx + 1}. {entry.title}
                      </span>
                    )}
                    {entry.summary_hint && (
                      <p className="text-xs mt-1 line-clamp-2" style={{ color: "var(--fg-secondary)" }}>
                        {entry.summary_hint}
                      </p>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
