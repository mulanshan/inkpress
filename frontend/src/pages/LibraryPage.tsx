import React, { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRightLeft,
  BookOpen,
  CalendarClock,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  List,
  Loader2,
  Rss,
  RotateCw,
  Send,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { apiRequest, copyToClipboard, formatBytes, formatRelativeTime } from "../lib/api";
import { useReaderAppearance } from "../lib/theme";
import type { BookExportResponse, BookItem, ExportTargetFormat } from "../lib/types";
import { ReaderSettingsBar } from "../components/ReaderSettingsBar";
import { useToast } from "../components/Toast";

interface ChapterMeta {
  index: number;
  filename: string;
  title: string;
}

interface PendingExportJob {
  bookId: number;
  bookTitle: string;
  exportKey: string;
  downloadUrl: string;
}

const BOOK_PROGRESS_KEY = "inkpress_book_chapter_v1";
const UPLOAD_ACCEPT = ".epub,.mobi,.azw3,.azw,.txt,.md,.markdown,.html,.htm,.docx,.pdf,.fb2,.cbz,.rtf";
const SIZE_LIMIT_OPTIONS: Array<{ label: string; value: number | null }> = [
  { label: "≤ 5 MB", value: 5 },
  { label: "≤ 10 MB（推荐）", value: 10 },
  { label: "≤ 15 MB", value: 15 },
  { label: "≤ 25 MB", value: 25 },
  { label: "不限体积", value: null },
];

const EXPORT_FORMAT_OPTIONS: Array<{
  id: ExportTargetFormat;
  title: string;
  badge: string;
  desc: string;
}> = [
  {
    id: "epub",
    title: "EPUB 电子书 (.epub)",
    badge: "墨水屏 / 在线读",
    desc: "文字自动重排分章；扫描版 PDF 自动生成高清墨水屏分页 EPUB3",
  },
  {
    id: "pdf",
    title: "轻量化 PDF (.pdf)",
    badge: "高压缩省空间",
    desc: "智能精简图片或墨水屏级黑白扫描高压缩，保留彩色封面与目录书签",
  },
  {
    id: "mobi",
    title: "Kindle MOBI (.mobi)",
    badge: "KF8 双内核",
    desc: "兼容旧款 Kindle 与墨水屏内置浏览器直连下载",
  },
  {
    id: "txt",
    title: "纯文本 TXT (.txt)",
    badge: "UTF-8",
    desc: "提取带章节结构的纯文本（需原书含可提取文字层）",
  },
];

function buildClientExportKey(fmt: ExportTargetFormat, maxMb: number | null): string {
  if (fmt === "txt" || maxMb === null) {
    return `${fmt}_unlimited`;
  }
  return `${fmt}_${maxMb}mb`;
}

function triggerBrowserDownload(url: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  window.setTimeout(() => {
    if (anchor.parentNode) {
      anchor.parentNode.removeChild(anchor);
    }
  }, 1500);
}

function loadSavedChapter(bookId: number): number {
  try {
    const raw = localStorage.getItem(BOOK_PROGRESS_KEY);
    if (!raw) return 0;
    const map = JSON.parse(raw) as Record<string, number>;
    const saved = map[String(bookId)];
    return typeof saved === "number" && saved >= 0 ? saved : 0;
  } catch {
    return 0;
  }
}

function saveBookChapter(bookId: number, idx: number): void {
  try {
    const raw = localStorage.getItem(BOOK_PROGRESS_KEY);
    const map = raw ? (JSON.parse(raw) as Record<string, number>) : {};
    map[String(bookId)] = idx;
    localStorage.setItem(BOOK_PROGRESS_KEY, JSON.stringify(map));
  } catch {
    // ignore storage errors
  }
}

function toLocalDatetimeInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function LibraryPage() {
  const qc = useQueryClient();
  const { notify } = useToast();
  const { fontFamily, fontSize, lineHeight } = useReaderAppearance();

  const [kindFilter, setKindFilter] = useState<string>("");
  const [uploading, setUploading] = useState(false);
  const [uploadKind, setUploadKind] = useState<"uploaded_book" | "magazine">("uploaded_book");
  const [readingBook, setReadingBook] = useState<BookItem | null>(null);
  const [chapterIdx, setChapterIdx] = useState(0);
  const [mobileTocOpen, setMobileTocOpen] = useState(false);
  const [reconvertingId, setReconvertingId] = useState<number | null>(null);
  const [scheduleBook, setScheduleBook] = useState<BookItem | null>(null);
  const [scheduleTime, setScheduleTime] = useState("");

  const [exportModalBook, setExportModalBook] = useState<BookItem | null>(null);
  const [exportFormat, setExportFormat] = useState<ExportTargetFormat>("epub");
  const [exportMaxMb, setExportMaxMb] = useState<number | null>(10);
  const [exportSetDefault, setExportSetDefault] = useState(false);
  const [exportSubmitting, setExportSubmitting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [pendingExports, setPendingExports] = useState<PendingExportJob[]>([]);
  const pollingBusyRef = useRef(false);

  const booksQuery = useQuery({
    queryKey: ["books", kindFilter],
    queryFn: () =>
      apiRequest<{ items: BookItem[] }>(
        `/api/v1/books${kindFilter ? `?kind=${kindFilter}` : ""}`,
      ),
    refetchInterval: (query) => {
      const list = query.state.data?.items ?? [];
      return list.some((b) => b.conversion_status === "converting") ? 3000 : false;
    },
  });

  const chaptersQuery = useQuery({
    queryKey: ["book-chapters", readingBook?.id],
    enabled: readingBook !== null && readingBook.can_read_online,
    queryFn: () =>
      apiRequest<{ book: BookItem; chapters: ChapterMeta[] }>(
        `/api/v1/books/${readingBook!.id}/chapters`,
      ),
  });

  const chapters = chaptersQuery.data?.chapters ?? [];

  // Clamp saved chapter index once chapters metadata loads
  useEffect(() => {
    if (!readingBook || chapters.length === 0) return;
    if (chapterIdx >= chapters.length) {
      setChapterIdx(0);
    }
  }, [readingBook, chapters.length, chapterIdx]);

  // Persist last-read chapter index per book.id
  useEffect(() => {
    if (!readingBook) return;
    saveBookChapter(readingBook.id, chapterIdx);
  }, [readingBook, chapterIdx]);

  const safeChapterIdx = chapters.length > 0 ? Math.min(chapterIdx, chapters.length - 1) : 0;

  const chapterContentQuery = useQuery({
    queryKey: ["book-chapter-content", readingBook?.id, safeChapterIdx],
    enabled:
      readingBook !== null &&
      readingBook.can_read_online &&
      chapters.length > 0,
    queryFn: () =>
      apiRequest<{ index: number; total: number; title: string; html: string }>(
        `/api/v1/books/${readingBook!.id}/chapters/${safeChapterIdx}`,
      ),
  });

  // Keyboard navigation in Web EPUB Reader: ArrowLeft / ArrowRight / Escape
  useEffect(() => {
    if (!readingBook) return;
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
        return;
      }
      if (e.key === "Escape") {
        if (mobileTocOpen) {
          setMobileTocOpen(false);
        } else {
          setReadingBook(null);
        }
      } else if (e.key === "ArrowLeft" && chapters.length > 0 && safeChapterIdx > 0) {
        e.preventDefault();
        setChapterIdx(safeChapterIdx - 1);
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else if (
        e.key === "ArrowRight" &&
        chapters.length > 0 &&
        safeChapterIdx < chapters.length - 1
      ) {
        e.preventDefault();
        setChapterIdx(safeChapterIdx + 1);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [readingBook, mobileTocOpen, chapters.length, safeChapterIdx]);

  const openBookReader = (book: BookItem) => {
    const saved = loadSavedChapter(book.id);
    setReadingBook(book);
    setChapterIdx(saved);
    setMobileTocOpen(false);
    window.scrollTo({ top: 0 });
  };

  const handleUploadFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    const list = Array.from(files);
    let succeeded = 0;
    const failedNames: string[] = [];
    let lastError = "";
    try {
      for (const file of list) {
        try {
          const fd = new FormData();
          fd.append("file", file);
          fd.append("kind", uploadKind);
          await apiRequest("/api/v1/books/upload", { method: "POST", body: fd });
          succeeded += 1;
        } catch (err) {
          failedNames.push(file.name);
          lastError = err instanceof Error ? err.message : "上传失败";
        }
      }
      if (succeeded > 0) {
        await qc.invalidateQueries({ queryKey: ["books"] });
      }
      if (failedNames.length === 0) {
        notify(`已上传 ${succeeded} 个文件，后台正在自动转码为 Kindle 标准 EPUB`, "success");
      } else if (succeeded > 0) {
        notify(
          `成功上传 ${succeeded} 个，${failedNames.length} 个失败（${failedNames[0]}：${lastError}）`,
          "error",
        );
      } else {
        notify(lastError || "上传失败", "error");
      }
    } finally {
      setUploading(false);
    }
  };

  const reconvertBook = async (book: BookItem) => {
    setReconvertingId(book.id);
    try {
      await apiRequest(`/api/v1/books/${book.id}/reconvert`, { method: "POST" });
      await qc.invalidateQueries({ queryKey: ["books"] });
      notify(`已重新加入转码队列：《${book.title}》`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "重试转码失败", "error");
    } finally {
      setReconvertingId(null);
    }
  };

  const deliverBook = async (book: BookItem) => {
    try {
      await apiRequest(`/api/v1/books/${book.id}/deliver`, {
        method: "POST",
        body: JSON.stringify({ target_email: "" }),
      });
      notify(`已加入推送队列：《${book.title}》`, "success");
      await qc.invalidateQueries({ queryKey: ["books"] });
    } catch (err) {
      notify(err instanceof Error ? err.message : "推送失败", "error");
    }
  };

  const saveSchedule = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!scheduleBook) return;
    try {
      await apiRequest(`/api/v1/books/${scheduleBook.id}/schedule`, {
        method: "PATCH",
        body: JSON.stringify({
          scheduled_push_at: scheduleTime ? new Date(scheduleTime).toISOString() : null,
        }),
      });
      setScheduleBook(null);
      await qc.invalidateQueries({ queryKey: ["books"] });
      notify("定时推送时间已更新", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "设置失败", "error");
    }
  };

  const deleteBook = async (book: BookItem) => {
    if (!window.confirm(`确认删除《${book.title}》及其文件？`)) return;
    try {
      await apiRequest(`/api/v1/books/${book.id}`, { method: "DELETE" });
      await qc.invalidateQueries({ queryKey: ["books"] });
      notify("已删除", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "删除失败", "error");
    }
  };

  const openExportModal = (book: BookItem) => {
    const activeFmt = (book.active_format || book.original_format || "epub").toLowerCase();
    setExportModalBook(book);
    setExportFormat(activeFmt === "pdf" ? "epub" : "pdf");
    setExportMaxMb(10);
    setExportSetDefault(false);
    setExportError("");
  };

  const submitExportDownload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!exportModalBook) return;
    setExportSubmitting(true);
    setExportError("");
    const targetBook = exportModalBook;
    try {
      const resp = await apiRequest<BookExportResponse>(`/api/v1/books/${targetBook.id}/export`, {
        method: "POST",
        body: JSON.stringify({
          target_format: exportFormat,
          max_mb: exportFormat === "txt" ? null : exportMaxMb,
          set_as_default:
            (exportFormat === "epub" || exportFormat === "pdf") ? exportSetDefault : false,
        }),
      });
      setExportModalBook(resp.item);
      await qc.invalidateQueries({ queryKey: ["books"] });

      if (resp.status === "ready") {
        triggerBrowserDownload(resp.download_url);
        const sizeLabel = resp.export?.size_bytes ? `（${formatBytes(resp.export.size_bytes)}）` : "";
        notify(`已准备就绪${sizeLabel}，开始下载`, "success");
        setExportModalBook(null);
      } else if (resp.status === "converting") {
        setPendingExports((prev) => {
          const filtered = prev.filter(
            (j) => !(j.bookId === targetBook.id && j.exportKey === resp.export_key),
          );
          return [
            ...filtered,
            {
              bookId: targetBook.id,
              bookTitle: targetBook.title,
              exportKey: resp.export_key,
              downloadUrl: resp.download_url,
            },
          ];
        });
      } else {
        const errMsg = resp.export?.error || "格式转换失败";
        setExportError(errMsg);
        notify(errMsg, "error");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "发起格式转换失败";
      setExportError(msg);
      notify(msg, "error");
    } finally {
      setExportSubmitting(false);
    }
  };

  useEffect(() => {
    if (pendingExports.length === 0) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      if (cancelled || pollingBusyRef.current) return;
      pollingBusyRef.current = true;
      try {
        for (const job of pendingExports) {
          if (cancelled) break;
          try {
            const res = await apiRequest<BookExportResponse>(
              `/api/v1/books/${job.bookId}/exports/${job.exportKey}`,
            );
            if (cancelled) break;
            if (res.status === "ready") {
              setPendingExports((prev) =>
                prev.filter(
                  (j) => !(j.bookId === job.bookId && j.exportKey === job.exportKey),
                ),
              );
              await qc.invalidateQueries({ queryKey: ["books"] });
              setExportModalBook((cur) => (cur && cur.id === job.bookId ? null : cur));
              triggerBrowserDownload(res.download_url);
              const sizeStr = res.export?.size_bytes
                ? `（${formatBytes(res.export.size_bytes)}）`
                : "";
              notify(`《${job.bookTitle}》转换完成${sizeStr}，已开始下载`, "success");
            } else if (res.status === "failed") {
              setPendingExports((prev) =>
                prev.filter(
                  (j) => !(j.bookId === job.bookId && j.exportKey === job.exportKey),
                ),
              );
              const errMsg = res.export?.error || "格式转换失败";
              setExportError(errMsg);
              notify(`《${job.bookTitle}》转换失败：${errMsg}`, "error");
            }
          } catch {
            // Transient network hiccup; retry on next poll tick
          }
        }
      } finally {
        pollingBusyRef.current = false;
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pendingExports, qc, notify]);

  const handleReaderContentClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const anchor = (e.target as HTMLElement | null)?.closest("a");
    if (!anchor) return;
    const idxAttr = anchor.getAttribute("data-chapter-index");
    if (idxAttr !== null) {
      e.preventDefault();
      const parsed = Number.parseInt(idxAttr, 10);
      if (!Number.isNaN(parsed) && parsed >= 0 && parsed < chapters.length) {
        setChapterIdx(parsed);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      return;
    }
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("#") || /^https?:\/\//i.test(href) || href.startsWith("mailto:")) {
      return;
    }
    const cleanHref = href.split("#")[0].replace(/^\.\//, "");
    const matched = chapters.find(
      (c) => c.filename === cleanHref || c.filename.endsWith(`/${cleanHref}`),
    );
    if (matched) {
      e.preventDefault();
      setChapterIdx(matched.index);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  };

  // Built-in Web EPUB Reader
  if (readingBook) {
    const cur = chapterContentQuery.data;
    const selectChapter = (idx: number) => {
      setChapterIdx(idx);
      setMobileTocOpen(false);
      window.scrollTo({ top: 0, behavior: "smooth" });
    };

    const tocList = (
      <div className="space-y-1">
        {chapters.map((ch) => (
          <button
            key={ch.index}
            type="button"
            onClick={() => selectChapter(ch.index)}
            className="w-full text-left text-xs px-2.5 py-2 rounded-lg cursor-pointer truncate"
            style={{
              backgroundColor:
                ch.index === safeChapterIdx ? "var(--accent-soft)" : "transparent",
              color: ch.index === safeChapterIdx ? "var(--accent)" : "var(--fg-secondary)",
              fontWeight: ch.index === safeChapterIdx ? 600 : 400,
            }}
          >
            {ch.title}
          </button>
        ))}
      </div>
    );

    return (
      <div className="max-w-5xl mx-auto pb-20">
        <div
          className="sticky top-[49px] md:top-0 z-20 flex flex-wrap items-center justify-between gap-3 py-3 mb-6 border-b backdrop-blur-sm"
          style={{
            backgroundColor: "color-mix(in srgb, var(--bg-canvas) 92%, transparent)",
            borderColor: "var(--border-hairline)",
          }}
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <button
              type="button"
              onClick={() => {
                setReadingBook(null);
                setMobileTocOpen(false);
              }}
              className="inline-flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg cursor-pointer shrink-0"
              style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
            >
              <ArrowLeft className="w-4 h-4" />
              <span>返回书架</span>
            </button>
            <button
              type="button"
              onClick={() => setMobileTocOpen((v) => !v)}
              className="lg:hidden inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer shrink-0"
              style={{
                borderColor: "var(--border-hairline)",
                backgroundColor: mobileTocOpen ? "var(--accent-soft)" : "var(--bg-surface)",
                color: mobileTocOpen ? "var(--accent)" : "var(--fg-secondary)",
              }}
            >
              <List className="w-3.5 h-3.5" />
              <span>目录 ({chapters.length})</span>
            </button>
            <h2 className="font-editorial text-lg font-semibold truncate max-w-[12rem] sm:max-w-md">
              {readingBook.title}
            </h2>
          </div>
          <ReaderSettingsBar />
        </div>

        {/* Collapsible Mobile TOC Drawer */}
        {mobileTocOpen && (
          <div className="lg:hidden fixed inset-0 z-40 bg-black/40 flex">
            <div
              className="w-72 max-w-[85vw] h-full border-r p-4 flex flex-col"
              style={{
                backgroundColor: "var(--bg-surface)",
                borderColor: "var(--border-hairline)",
              }}
            >
              <div
                className="flex items-center justify-between pb-3 mb-3 border-b"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                <span
                  className="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: "var(--fg-muted)" }}
                >
                  目录 · {chapters.length} 章
                </span>
                <button
                  type="button"
                  onClick={() => setMobileTocOpen(false)}
                  className="p-1 rounded cursor-pointer"
                  style={{ color: "var(--fg-muted)" }}
                  aria-label="关闭目录"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex-1 overflow-y-auto">{tocList}</div>
            </div>
            <div className="flex-1" onClick={() => setMobileTocOpen(false)} />
          </div>
        )}

        {chaptersQuery.isLoading ? (
          <div className="py-24 text-center text-sm" style={{ color: "var(--fg-muted)" }}>
            正在解析目录…
          </div>
        ) : chaptersQuery.isError ? (
          <div
            className="rounded-2xl border p-10 text-center space-y-4"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-danger)",
            }}
          >
            <AlertCircle className="w-7 h-7 mx-auto" style={{ color: "var(--fg-danger)" }} />
            <div>
              <p className="font-editorial text-xl font-semibold mb-1">无法加载图书目录</p>
              <p className="text-xs" style={{ color: "var(--fg-danger)" }}>
                {chaptersQuery.error instanceof Error
                  ? chaptersQuery.error.message
                  : "读取 EPUB 结构失败"}
              </p>
            </div>
            <div className="flex items-center justify-center gap-2">
              <button
                type="button"
                onClick={() => chaptersQuery.refetch()}
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                重试
              </button>
              <button
                type="button"
                onClick={() => setReadingBook(null)}
                className="px-4 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)", color: "var(--fg-secondary)" }}
              >
                返回书架
              </button>
            </div>
          </div>
        ) : chapters.length === 0 ? (
          <div
            className="rounded-2xl border p-12 text-center space-y-4"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <BookOpen className="w-8 h-8 mx-auto" style={{ color: "var(--fg-muted)" }} />
            <div>
              <p className="font-editorial text-xl font-semibold mb-1">未检测到可读章节</p>
              <p className="text-sm" style={{ color: "var(--fg-secondary)" }}>
                该电子书不包含可在线排版的章节内容，建议直接下载文件或推送至 Kindle 阅读。
              </p>
            </div>
            <button
              type="button"
              onClick={() => setReadingBook(null)}
              className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
              style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
            >
              返回书架
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <aside className="hidden lg:block lg:col-span-3">
              <div
                className="rounded-xl border p-4 max-h-[72vh] overflow-y-auto sticky top-20"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                }}
              >
                <div
                  className="text-xs font-semibold uppercase tracking-wider mb-3"
                  style={{ color: "var(--fg-muted)" }}
                >
                  目录 · {chapters.length} 章
                </div>
                {tocList}
              </div>
            </aside>

            <section className="lg:col-span-9">
              {chapterContentQuery.isError ? (
                <div
                  className="rounded-2xl border p-10 text-center space-y-4"
                  style={{
                    backgroundColor: "var(--bg-surface)",
                    borderColor: "var(--border-danger)",
                  }}
                >
                  <AlertCircle className="w-7 h-7 mx-auto" style={{ color: "var(--fg-danger)" }} />
                  <div>
                    <p className="font-editorial text-xl font-semibold mb-1">章节加载失败</p>
                    <p className="text-xs" style={{ color: "var(--fg-danger)" }}>
                      {chapterContentQuery.error instanceof Error
                        ? chapterContentQuery.error.message
                        : "请稍后重试"}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => chapterContentQuery.refetch()}
                    className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                    style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                  >
                    重新加载本章
                  </button>
                </div>
              ) : chapterContentQuery.isLoading || !cur ? (
                <div className="py-20 text-center text-sm" style={{ color: "var(--fg-muted)" }}>
                  正在排版章节…
                </div>
              ) : (
                <div className="mx-auto" style={{ maxWidth: "68ch" }}>
                  <div
                    onClick={handleReaderContentClick}
                    className={`editorial-prose ${fontFamily === "serif" ? "font-editorial" : "font-sans-clean"}`}
                    style={{ fontSize: `${fontSize}px`, lineHeight }}
                    dangerouslySetInnerHTML={{ __html: cur.html }}
                  />
                  <div
                    className="flex items-center justify-between pt-8 mt-12 border-t"
                    style={{ borderColor: "var(--border-hairline)" }}
                  >
                    <button
                      type="button"
                      disabled={safeChapterIdx <= 0}
                      onClick={() => selectChapter(Math.max(0, safeChapterIdx - 1))}
                      className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border text-sm cursor-pointer disabled:opacity-40"
                      style={{
                        borderColor: "var(--border-hairline)",
                        backgroundColor: "var(--bg-surface)",
                      }}
                    >
                      <ChevronLeft className="w-4 h-4" />
                      <span>上一章</span>
                    </button>
                    <span className="text-xs tabular-nums" style={{ color: "var(--fg-muted)" }}>
                      {safeChapterIdx + 1} / {chapters.length}
                    </span>
                    <button
                      type="button"
                      disabled={safeChapterIdx >= chapters.length - 1}
                      onClick={() =>
                        selectChapter(Math.min(chapters.length - 1, safeChapterIdx + 1))
                      }
                      className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border text-sm cursor-pointer disabled:opacity-40"
                      style={{
                        borderColor: "var(--border-hairline)",
                        backgroundColor: "var(--bg-surface)",
                      }}
                    >
                      <span>下一章</span>
                      <ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    );
  }

  const books = booksQuery.data?.items ?? [];

  return (
    <div className="max-w-5xl mx-auto pb-16">
      <header className="pb-6 mb-7 border-b-2" style={{ borderColor: "var(--fg-primary)" }}>
        <p className="text-xs uppercase tracking-[0.2em] mb-1.5" style={{ color: "var(--accent)" }}>
          PERSONAL LIBRARY &amp; ARCHIVE
        </p>
        <h1 className="font-editorial text-4xl font-semibold">书架与杂志 · Library</h1>
        <p className="text-sm mt-2" style={{ color: "var(--fg-secondary)" }}>
          自动归档每期生成的日报与杂志，也支持上传个人电子书（EPUB / MOBI / AZW3 / TXT / MD / DOCX / PDF / FB2 / CBZ / RTF）自动修复转码并定时推送至 Kindle。
        </p>
        <div className="flex flex-wrap items-center gap-2 mt-4 text-xs">
          <button
            type="button"
            onClick={async () => {
              const url = `${window.location.origin}/api/v1/opds`;
              const ok = await copyToClipboard(url);
              notify(ok ? `已复制 OPDS 1.2 订阅地址：${url}` : `OPDS 地址：${url}`, ok ? "success" : "info");
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border cursor-pointer"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
              color: "var(--fg-secondary)",
            }}
            title="供 KOReader、文石 Boox、Foliate 等墨水屏阅读器直连订阅"
          >
            <Rss className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />
            <span>复制 OPDS 1.2 书库源</span>
            <Copy className="w-3 h-3 opacity-70" />
          </button>
          <a
            href="/kindle"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
              color: "var(--fg-secondary)",
            }}
            title="无 JS 极简高对比 HTML 页，适合 Kindle 内置浏览器直连下载"
          >
            <span>/kindle 墨水屏极简直下页</span>
            <ExternalLink className="w-3 h-3 opacity-70" />
          </a>
        </div>

        <div className="flex flex-wrap items-center gap-2 mt-5">
          {[
            { id: "", label: "全部馆藏" },
            { id: "edition_issue", label: "往期日报" },
            { id: "uploaded_book", label: "个人图书" },
            { id: "magazine", label: "电子杂志" },
            { id: "web_clip", label: "网页剪藏" },
          ].map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setKindFilter(tab.id)}
              className="text-xs px-3 py-1 rounded-full border cursor-pointer"
              style={{
                backgroundColor:
                  kindFilter === tab.id ? "var(--fg-primary)" : "var(--bg-surface)",
                color: kindFilter === tab.id ? "var(--bg-canvas)" : "var(--fg-secondary)",
                borderColor: "var(--border-hairline)",
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </header>

      {/* Drag & drop upload zone */}
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          handleUploadFiles(e.dataTransfer.files);
        }}
        className="rounded-2xl border-2 border-dashed p-6 mb-8 flex flex-col sm:flex-row items-center justify-between gap-4"
        style={{
          backgroundColor: "var(--bg-surface)",
          borderColor: "var(--border-hairline)",
        }}
      >
        <div className="flex items-center gap-3.5">
          <div
            className="w-11 h-11 rounded-xl flex items-center justify-center shrink-0"
            style={{ backgroundColor: "var(--accent-soft)", color: "var(--accent)" }}
          >
            <Upload className="w-5 h-5" />
          </div>
          <div>
            <div className="font-medium text-sm">
              拖拽电子书或杂志文件到此处上传，自动转为零 E999 Kindle EPUB3
            </div>
            <div className="text-xs mt-0.5" style={{ color: "var(--fg-muted)" }}>
              支持 EPUB（自动修复）/ MOBI / AZW3 / TXT / Markdown / HTML / DOCX / PDF / FB2 / CBZ / RTF
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <select
            value={uploadKind}
            onChange={(e) => setUploadKind(e.target.value as "uploaded_book" | "magazine")}
            className="rounded-lg border px-2.5 py-2 text-xs"
            style={{
              backgroundColor: "var(--bg-canvas)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <option value="uploaded_book">归类：图书</option>
            <option value="magazine">归类：杂志</option>
          </select>
          <label
            className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-medium cursor-pointer"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
          >
            <span>{uploading ? "上传中…" : "选择文件"}</span>
            <input
              type="file"
              multiple
              accept={UPLOAD_ACCEPT}
              className="hidden"
              disabled={uploading}
              onChange={(e) => {
                const picked = e.target.files;
                handleUploadFiles(picked);
                e.target.value = "";
              }}
            />
          </label>
        </div>
      </div>

      {/* Book Cover Grid */}
      {booksQuery.isError ? (
        <div
          className="rounded-2xl border p-10 text-center space-y-3"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-danger)",
          }}
        >
          <AlertCircle className="w-7 h-7 mx-auto" style={{ color: "var(--fg-danger)" }} />
          <p className="font-editorial text-xl font-semibold">书架加载失败</p>
          <p className="text-xs" style={{ color: "var(--fg-danger)" }}>
            {booksQuery.error instanceof Error ? booksQuery.error.message : "请稍后重试"}
          </p>
          <button
            type="button"
            onClick={() => booksQuery.refetch()}
            className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
          >
            重试
          </button>
        </div>
      ) : books.length === 0 ? (
        <div
          className="rounded-2xl border p-12 text-center text-sm"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-hairline)",
            color: "var(--fg-muted)",
          }}
        >
          书架为空，上传你的第一本电子书，或在「发行工作室」生成一期日报吧。
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {books.map((book) => {
            const activeFmt = (book.active_format || book.original_format || "epub").toLowerCase();
            const isPdfDownload = !book.can_read_online && activeFmt === "pdf";
            const isExportingBook = pendingExports.some((j) => j.bookId === book.id);
            const badgeLabel =
              book.conversion_status === "ready"
                ? book.original_format.toLowerCase() === "pdf" && activeFmt === "epub"
                  ? "PDF · EPUB"
                  : book.original_format.toUpperCase()
                : book.conversion_status === "converting"
                  ? "转码中…"
                  : "转码失败";
            return (
              <div
                key={book.id}
                className="rounded-2xl border overflow-hidden flex flex-col justify-between"
                style={{
                  backgroundColor: "var(--bg-surface)",
                  borderColor: "var(--border-hairline)",
                  boxShadow: "var(--shadow-paper)",
                }}
              >
                <div>
                  <div
                    className="aspect-[3/4] max-h-64 w-full overflow-hidden relative flex items-center justify-center border-b"
                    style={{
                      backgroundColor: "var(--bg-subtle)",
                      borderColor: "var(--border-hairline)",
                    }}
                  >
                    {book.has_cover ? (
                      <img
                        src={`/api/v1/books/${book.id}/cover`}
                        alt={book.title}
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <div className="p-6 text-center">
                        <BookOpen className="w-8 h-8 mx-auto mb-2" style={{ color: "var(--accent)" }} />
                        <div className="font-editorial text-lg font-semibold line-clamp-3">
                          {book.title}
                        </div>
                      </div>
                    )}
                    <span
                      className="absolute top-2.5 right-2.5 text-[11px] font-medium px-2 py-0.5 rounded-full border"
                      style={{
                        backgroundColor:
                          book.conversion_status === "ready"
                            ? "var(--fg-primary)"
                            : book.conversion_status === "failed"
                              ? "var(--bg-danger-soft)"
                              : "var(--accent)",
                        color:
                          book.conversion_status === "ready"
                            ? "var(--bg-canvas)"
                            : book.conversion_status === "failed"
                              ? "var(--fg-danger)"
                              : "var(--accent-fg)",
                        borderColor:
                          book.conversion_status === "failed"
                            ? "var(--border-danger)"
                            : "transparent",
                      }}
                    >
                      {badgeLabel}
                    </span>
                  </div>

                  <div className="p-4">
                    <h3 className="font-editorial text-lg font-semibold leading-snug line-clamp-2 mb-1">
                      {book.title}
                    </h3>
                    <p className="text-xs mb-2" style={{ color: "var(--fg-secondary)" }}>
                      {book.author || "InkPress"} · {formatBytes(book.file_size_bytes)} ·{" "}
                      {formatRelativeTime(book.created_at)}
                    </p>
                    {book.conversion_error && (
                      <p
                        className="text-xs line-clamp-2 mb-2 rounded-md px-2 py-1 border"
                        style={{
                          color: "var(--fg-danger)",
                          backgroundColor: "var(--bg-danger-soft)",
                          borderColor: "var(--border-danger)",
                        }}
                      >
                        {book.conversion_error}
                      </p>
                    )}
                    {book.scheduled_push_at && (
                      <p className="text-xs mb-2" style={{ color: "var(--accent)" }}>
                        定时推送：{new Date(book.scheduled_push_at).toLocaleString("zh-CN")}
                      </p>
                    )}
                  </div>
                </div>

                <div
                  className="px-4 py-3 border-t flex flex-wrap items-center justify-between gap-2 text-xs"
                  style={{ borderColor: "var(--border-hairline)" }}
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    {book.conversion_status === "failed" ? (
                      <button
                        type="button"
                        disabled={reconvertingId === book.id}
                        onClick={() => reconvertBook(book)}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border font-medium cursor-pointer disabled:opacity-50"
                        style={{
                          borderColor: "var(--border-danger)",
                          backgroundColor: "var(--bg-danger-soft)",
                          color: "var(--fg-danger)",
                        }}
                      >
                        <RotateCw
                          className={`w-3.5 h-3.5 ${reconvertingId === book.id ? "animate-spin" : ""}`}
                        />
                        <span>{reconvertingId === book.id ? "重试中…" : "重试转码"}</span>
                      </button>
                    ) : (
                      <>
                        {book.can_read_online && book.conversion_status === "ready" && (
                          <button
                            type="button"
                            onClick={() => openBookReader(book)}
                            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border cursor-pointer"
                            style={{
                              borderColor: "var(--border-hairline)",
                              backgroundColor: "var(--bg-canvas)",
                            }}
                          >
                            <BookOpen className="w-3.5 h-3.5" />
                            <span>阅读</span>
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={book.conversion_status !== "ready"}
                          onClick={() => deliverBook(book)}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg font-medium cursor-pointer disabled:opacity-40"
                          style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                        >
                          <Send className="w-3.5 h-3.5" />
                          <span>推 Kindle</span>
                        </button>
                        {book.conversion_status === "ready" && (
                          <button
                            type="button"
                            onClick={() => openExportModal(book)}
                            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border cursor-pointer"
                            style={{
                              borderColor: isExportingBook
                                ? "var(--accent)"
                                : "var(--border-hairline)",
                              backgroundColor: isExportingBook
                                ? "var(--accent-soft)"
                                : "var(--bg-canvas)",
                              color: isExportingBook ? "var(--accent)" : "var(--fg-primary)",
                            }}
                            title="转换为 EPUB、10MB 以内轻量化 PDF 等其他格式下载"
                          >
                            {isExportingBook ? (
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                              <ArrowRightLeft className="w-3.5 h-3.5" />
                            )}
                            <span>{isExportingBook ? "转换中…" : "转换下载"}</span>
                          </button>
                        )}
                      </>
                    )}
                  </div>

                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => {
                        setScheduleBook(book);
                        setScheduleTime(toLocalDatetimeInput(book.scheduled_push_at));
                      }}
                      className="p-1.5 rounded-lg hover:opacity-80 cursor-pointer"
                      style={{ color: "var(--fg-secondary)" }}
                      title="设置定时推送"
                    >
                      <CalendarClock className="w-4 h-4" />
                    </button>
                    {book.conversion_status === "ready" && (
                      <a
                        href={`/api/v1/books/${book.id}/download`}
                        className="p-1.5 rounded-lg hover:opacity-80"
                        style={{ color: "var(--fg-secondary)" }}
                        title={isPdfDownload ? "直接下载当前 PDF" : "直接下载当前 EPUB"}
                      >
                        <Download className="w-4 h-4" />
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => deleteBook(book)}
                      className="p-1.5 rounded-lg hover:opacity-80 cursor-pointer"
                      style={{ color: "var(--fg-muted)" }}
                      title="删除"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Convert to other formats & compress download modal */}
      {exportModalBook && (() => {
        const latestModalBook =
          books.find((b) => b.id === exportModalBook.id) ?? exportModalBook;
        const selectedKey = buildClientExportKey(exportFormat, exportMaxMb);
        const cachedInfo = latestModalBook.exports?.[selectedKey];
        const isCachedReady = cachedInfo?.status === "ready" && cachedInfo.size_bytes > 0;
        const isJobRunning = pendingExports.some(
          (j) => j.bookId === latestModalBook.id && j.exportKey === selectedKey,
        );
        const curActiveFmt = (
          latestModalBook.active_format ||
          latestModalBook.original_format ||
          "epub"
        ).toUpperCase();

        return (
          <div
            className="fixed inset-0 z-40 bg-black/45 flex items-center justify-center p-4 overflow-y-auto"
            onClick={(e) => {
              if (e.target === e.currentTarget && !exportSubmitting) {
                setExportModalBook(null);
              }
            }}
          >
            <form
              onSubmit={submitExportDownload}
              className="max-w-lg w-full rounded-2xl border p-6 space-y-5 my-8"
              style={{
                backgroundColor: "var(--bg-surface)",
                borderColor: "var(--border-hairline)",
                boxShadow: "var(--shadow-paper)",
              }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p
                    className="text-[11px] uppercase tracking-[0.16em] mb-1"
                    style={{ color: "var(--accent)" }}
                  >
                    FORMAT CONVERTER &amp; SIZE OPTIMIZER
                  </p>
                  <h3 className="font-editorial text-xl font-semibold">转换其他格式下载</h3>
                  <p
                    className="text-xs mt-1 truncate"
                    style={{ color: "var(--fg-secondary)" }}
                    title={latestModalBook.title}
                  >
                    《{latestModalBook.title}》 · 当前 {curActiveFmt} ·{" "}
                    {formatBytes(latestModalBook.file_size_bytes)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setExportModalBook(null)}
                  className="p-1.5 rounded-lg cursor-pointer shrink-0"
                  style={{ color: "var(--fg-muted)" }}
                  aria-label="关闭窗口"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div>
                <label
                  className="block text-xs font-medium mb-2"
                  style={{ color: "var(--fg-secondary)" }}
                >
                  选择目标下载格式
                </label>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  {EXPORT_FORMAT_OPTIONS.map((opt) => {
                    const selected = exportFormat === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => {
                          setExportFormat(opt.id);
                          setExportError("");
                        }}
                        className="text-left rounded-xl border p-3 cursor-pointer flex flex-col justify-between gap-1.5"
                        style={{
                          borderColor: selected ? "var(--accent)" : "var(--border-hairline)",
                          backgroundColor: selected ? "var(--accent-soft)" : "var(--bg-canvas)",
                        }}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-xs">{opt.title}</span>
                          <span
                            className="text-[10px] px-1.5 py-0.5 rounded-md border shrink-0"
                            style={{
                              borderColor: "var(--border-hairline)",
                              backgroundColor: "var(--bg-surface)",
                              color: selected ? "var(--accent)" : "var(--fg-muted)",
                            }}
                          >
                            {opt.badge}
                          </span>
                        </div>
                        <p className="text-[11px] leading-relaxed" style={{ color: "var(--fg-secondary)" }}>
                          {opt.desc}
                        </p>
                      </button>
                    );
                  })}
                </div>
              </div>

              {exportFormat !== "txt" && (
                <div>
                  <label
                    className="block text-xs font-medium mb-2"
                    style={{ color: "var(--fg-secondary)" }}
                  >
                    文件体积上限控制
                  </label>
                  <div className="flex flex-wrap gap-1.5">
                    {SIZE_LIMIT_OPTIONS.map((opt) => {
                      const active = exportMaxMb === opt.value;
                      return (
                        <button
                          key={String(opt.value)}
                          type="button"
                          onClick={() => {
                            setExportMaxMb(opt.value);
                            setExportError("");
                          }}
                          className="text-xs px-3 py-1.5 rounded-lg border cursor-pointer"
                          style={{
                            borderColor: active ? "var(--accent)" : "var(--border-hairline)",
                            backgroundColor: active ? "var(--fg-primary)" : "var(--bg-canvas)",
                            color: active ? "var(--bg-canvas)" : "var(--fg-secondary)",
                            fontWeight: active ? 600 : 400,
                          }}
                        >
                          {opt.label}
                        </button>
                      );
                    })}
                  </div>
                  <p className="text-[11px] mt-1.5" style={{ color: "var(--fg-muted)" }}>
                    {isCachedReady ? (
                      <span className="inline-flex items-center gap-1" style={{ color: "var(--accent)" }}>
                        <Check className="w-3.5 h-3.5" />
                        <span>
                          已生成该规格缓存（{formatBytes(cachedInfo.size_bytes)}），点击下方按钮即可秒速下载
                        </span>
                      </span>
                    ) : (
                      "自适应采样调控图像分辨率与墨水屏二值化清晰度，自动将输出体积压入目标上限以内。"
                    )}
                  </p>
                </div>
              )}

              {(exportFormat === "epub" || exportFormat === "pdf") && (
                <label
                  className="flex items-start gap-2.5 rounded-xl border p-3 text-xs cursor-pointer"
                  style={{
                    borderColor: "var(--border-hairline)",
                    backgroundColor: "var(--bg-canvas)",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={exportSetDefault}
                    onChange={(e) => setExportSetDefault(e.target.checked)}
                    className="mt-0.5 accent-[var(--accent)]"
                  />
                  <span style={{ color: "var(--fg-secondary)" }}>
                    同时设为该书默认文件（更新卡片体积，供「阅读」、「推 Kindle」与默认下载直接使用；原始上传文件仍安全保留）
                  </span>
                </label>
              )}

              {isJobRunning && (
                <div
                  className="rounded-xl border px-3.5 py-3 text-xs flex items-start gap-2.5"
                  style={{
                    borderColor: "var(--accent)",
                    backgroundColor: "var(--accent-soft)",
                    color: "var(--fg-primary)",
                  }}
                >
                  <Loader2 className="w-4 h-4 animate-spin shrink-0 mt-0.5" style={{ color: "var(--accent)" }} />
                  <div>
                    <div className="font-medium">正在后台转换并压缩体积…</div>
                    <div className="mt-0.5" style={{ color: "var(--fg-secondary)" }}>
                      多页扫描文档约需 10～25 秒，完成后将自动弹出浏览器下载（即使关闭此窗口也会自动下载）。
                    </div>
                  </div>
                </div>
              )}

              {exportError && (
                <div
                  className="rounded-xl border px-3.5 py-2.5 text-xs"
                  style={{
                    borderColor: "var(--border-danger)",
                    backgroundColor: "var(--bg-danger-soft)",
                    color: "var(--fg-danger)",
                  }}
                >
                  {exportError}
                </div>
              )}

              <div
                className="pt-3 border-t flex flex-wrap items-center justify-between gap-3"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                <div>
                  {latestModalBook.has_original && (
                    <a
                      href={`/api/v1/books/${latestModalBook.id}/download?variant=original`}
                      className="inline-flex items-center gap-1 text-xs underline underline-offset-2 hover:opacity-80"
                      style={{ color: "var(--fg-muted)" }}
                    >
                      <Download className="w-3.5 h-3.5" />
                      <span>
                        下载原始上传文件 (.{latestModalBook.original_format.toUpperCase()}
                        {latestModalBook.original_size_bytes
                          ? ` · ${formatBytes(latestModalBook.original_size_bytes)}`
                          : ""}
                        )
                      </span>
                    </a>
                  )}
                </div>

                <div className="flex items-center gap-2 ml-auto">
                  <button
                    type="button"
                    onClick={() => setExportModalBook(null)}
                    className="px-3.5 py-2 rounded-lg text-xs border cursor-pointer"
                    style={{ borderColor: "var(--border-hairline)" }}
                  >
                    关闭
                  </button>
                  <button
                    type="submit"
                    disabled={exportSubmitting || isJobRunning}
                    className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-50"
                    style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
                  >
                    {exportSubmitting || isJobRunning ? (
                      <>
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        <span>正在转换…</span>
                      </>
                    ) : isCachedReady && !exportSetDefault ? (
                      <>
                        <Download className="w-3.5 h-3.5" />
                        <span>立即下载 ({formatBytes(cachedInfo.size_bytes)})</span>
                      </>
                    ) : (
                      <>
                        <Download className="w-3.5 h-3.5" />
                        <span>转换并下载</span>
                      </>
                    )}
                  </button>
                </div>
              </div>
            </form>
          </div>
        );
      })()}

      {/* Schedule push modal */}
      {scheduleBook && (
        <div className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center p-4">
          <form
            onSubmit={saveSchedule}
            className="max-w-sm w-full rounded-2xl border p-6 space-y-4"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
            }}
          >
            <h3 className="font-editorial text-xl font-semibold">定时推送至 Kindle</h3>
            <p className="text-xs" style={{ color: "var(--fg-secondary)" }}>
              书目：《{scheduleBook.title}》
            </p>
            <div>
              <label className="block text-xs mb-1.5" style={{ color: "var(--fg-secondary)" }}>
                推送时间（留空则清除排期）
              </label>
              <input
                type="datetime-local"
                value={scheduleTime}
                onChange={(e) => setScheduleTime(e.target.value)}
                className="w-full rounded-lg border px-3 py-2 text-sm"
                style={{
                  backgroundColor: "var(--bg-canvas)",
                  borderColor: "var(--border-hairline)",
                }}
              />
            </div>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setScheduleBook(null)}
                className="px-3.5 py-2 rounded-lg text-xs border cursor-pointer"
                style={{ borderColor: "var(--border-hairline)" }}
              >
                取消
              </button>
              <button
                type="submit"
                className="px-4 py-2 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
              >
                保存排期
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
