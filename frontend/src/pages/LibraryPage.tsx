import React, { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  BookOpen,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Download,
  Send,
  Trash2,
  Upload,
} from "lucide-react";
import { apiRequest, formatBytes, formatRelativeTime } from "../lib/api";
import { useReaderAppearance } from "../lib/theme";
import type { BookItem } from "../lib/types";
import { ReaderSettingsBar } from "../components/ReaderSettingsBar";
import { useToast } from "../components/Toast";

interface ChapterMeta {
  index: number;
  filename: string;
  title: string;
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
  const [scheduleBook, setScheduleBook] = useState<BookItem | null>(null);
  const [scheduleTime, setScheduleTime] = useState("");

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

  const chapterContentQuery = useQuery({
    queryKey: ["book-chapter-content", readingBook?.id, chapterIdx],
    enabled:
      readingBook !== null &&
      readingBook.can_read_online &&
      (chaptersQuery.data?.chapters.length ?? 0) > 0,
    queryFn: () =>
      apiRequest<{ index: number; total: number; title: string; html: string }>(
        `/api/v1/books/${readingBook!.id}/chapters/${chapterIdx}`,
      ),
  });

  const handleUploadFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("kind", uploadKind);
        await apiRequest("/api/v1/books/upload", { method: "POST", body: fd });
      }
      await qc.invalidateQueries({ queryKey: ["books"] });
      notify(`已上传 ${files.length} 个文件，后台正在自动转码为 Kindle 标准 EPUB`, "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "上传失败", "error");
    } finally {
      setUploading(false);
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

  // Built-in Web EPUB Reader
  if (readingBook) {
    const chapters = chaptersQuery.data?.chapters ?? [];
    const cur = chapterContentQuery.data;
    return (
      <div className="max-w-5xl mx-auto pb-20">
        <div
          className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 py-3 mb-6 border-b backdrop-blur-sm"
          style={{
            backgroundColor: "color-mix(in srgb, var(--bg-canvas) 92%, transparent)",
            borderColor: "var(--border-hairline)",
          }}
        >
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => {
                setReadingBook(null);
                setChapterIdx(0);
              }}
              className="inline-flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg cursor-pointer"
              style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-secondary)" }}
            >
              <ArrowLeft className="w-4 h-4" />
              <span>返回书架</span>
            </button>
            <h2 className="font-editorial text-lg font-semibold truncate max-w-xs sm:max-w-md">
              {readingBook.title}
            </h2>
          </div>
          <ReaderSettingsBar />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
          <aside className="lg:col-span-3">
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
              <div className="space-y-1">
                {chapters.map((ch) => (
                  <button
                    key={ch.index}
                    type="button"
                    onClick={() => {
                      setChapterIdx(ch.index);
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                    className="w-full text-left text-xs px-2.5 py-2 rounded-lg cursor-pointer truncate"
                    style={{
                      backgroundColor:
                        ch.index === chapterIdx ? "var(--accent-soft)" : "transparent",
                      color: ch.index === chapterIdx ? "var(--accent)" : "var(--fg-secondary)",
                      fontWeight: ch.index === chapterIdx ? 600 : 400,
                    }}
                  >
                    {ch.title}
                  </button>
                ))}
              </div>
            </div>
          </aside>

          <section className="lg:col-span-9">
            {chapterContentQuery.isLoading || !cur ? (
              <div className="py-20 text-center text-sm" style={{ color: "var(--fg-muted)" }}>
                正在排版章节…
              </div>
            ) : (
              <div className="mx-auto" style={{ maxWidth: "68ch" }}>
                <div
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
                    disabled={chapterIdx <= 0}
                    onClick={() => {
                      setChapterIdx((i) => Math.max(0, i - 1));
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
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
                    {chapterIdx + 1} / {chapters.length}
                  </span>
                  <button
                    type="button"
                    disabled={chapterIdx >= chapters.length - 1}
                    onClick={() => {
                      setChapterIdx((i) => Math.min(chapters.length - 1, i + 1));
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
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
          自动归档每期生成的日报与杂志，也支持上传个人电子书（EPUB / MOBI / AZW3 / TXT / MD / DOCX / PDF / CBZ）自动修复转码并定时推送至 Kindle。
        </p>

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
              支持 EPUB（自动修复）/ MOBI / AZW3 / TXT / Markdown / HTML / DOCX / PDF / FB2 / CBZ
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
              className="hidden"
              disabled={uploading}
              onChange={(e) => handleUploadFiles(e.target.files)}
            />
          </label>
        </div>
      </div>

      {/* Book Cover Grid */}
      {books.length === 0 ? (
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
          {books.map((book) => (
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
                    className="absolute top-2.5 right-2.5 text-[11px] font-medium px-2 py-0.5 rounded-full"
                    style={{
                      backgroundColor:
                        book.conversion_status === "ready"
                          ? "var(--fg-primary)"
                          : book.conversion_status === "failed"
                            ? "#991b1b"
                            : "var(--accent)",
                      color: "#ffffff",
                    }}
                  >
                    {book.conversion_status === "ready"
                      ? book.original_format.toUpperCase()
                      : book.conversion_status === "converting"
                        ? "转码中…"
                        : "转码失败"}
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
                    <p className="text-xs text-red-700 line-clamp-2 mb-2">
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
                <div className="flex items-center gap-1.5">
                  {book.can_read_online && book.conversion_status === "ready" && (
                    <button
                      type="button"
                      onClick={() => {
                        setReadingBook(book);
                        setChapterIdx(0);
                      }}
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
                </div>

                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      setScheduleBook(book);
                      setScheduleTime("");
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
                      title="下载 EPUB"
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
          ))}
        </div>
      )}

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
