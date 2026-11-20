export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, {
    credentials: "include",
    ...init,
    headers,
  });
  if (!resp.ok) {
    let msg = `请求失败 (${resp.status})`;
    try {
      const data = (await resp.json()) as { detail?: string };
      if (data.detail) msg = data.detail;
    } catch {
      // ignore non-json error body
    }
    throw new ApiError(msg, resp.status);
  }
  if (resp.status === 204) {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

export function isSafeHttpUrl(raw: string | null | undefined): boolean {
  if (!raw) return false;
  try {
    const u = new URL(raw.trim());
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export function mediaProxyUrl(rawUrl: string, referer?: string | null): string {
  const trimmed = rawUrl.trim();
  if (!trimmed) return trimmed;
  if (trimmed.startsWith("/api/v1/media/proxy")) return trimmed;
  if (!isSafeHttpUrl(trimmed)) return trimmed;
  try {
    const parsed = new URL(trimmed);
    if (parsed.hostname === "inkpress.local") return trimmed;
  } catch {
    return trimmed;
  }
  const params = new URLSearchParams();
  params.set("url", trimmed);
  if (referer && isSafeHttpUrl(referer)) {
    try {
      const refParsed = new URL(referer);
      if (refParsed.hostname !== "inkpress.local") {
        params.set("referer", referer.trim());
      }
    } catch {
      // ignore invalid referer
    }
  }
  return `/api/v1/media/proxy?${params.toString()}`;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to textarea fallback
    }
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-9999px";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatRelativeTime(iso: string | null): string {
  if (!iso) return "尚未";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "尚未";
  const diffSec = Math.floor((Date.now() - dt.getTime()) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)} 天前`;
  return dt.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}
