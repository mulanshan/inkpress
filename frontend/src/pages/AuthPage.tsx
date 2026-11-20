import React, { useState } from "react";
import { BookOpen, ArrowRight } from "lucide-react";
import { apiRequest } from "../lib/api";
import type { User } from "../lib/types";
import { ReaderSettingsBar } from "../components/ReaderSettingsBar";

interface AuthPageProps {
  initialized: boolean;
  onSuccess: (user: User) => void;
}

export function AuthPage({ initialized, onSuccess }: AuthPageProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("Reader");
  const [kindleEmail, setKindleEmail] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (initialized) {
        const res = await apiRequest<{ user: User }>("/api/v1/auth/login", {
          method: "POST",
          body: JSON.stringify({ username, password }),
        });
        onSuccess(res.user);
      } else {
        const res = await apiRequest<{ user: User }>("/api/v1/auth/bootstrap", {
          method: "POST",
          body: JSON.stringify({
            username,
            password,
            display_name: displayName,
            kindle_email: kindleEmail,
          }),
        });
        onSuccess(res.user);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col justify-between px-4 py-8">
      <div className="flex justify-end max-w-4xl w-full mx-auto">
        <ReaderSettingsBar compact />
      </div>

      <main className="max-w-md w-full mx-auto my-auto">
        <div className="text-center mb-8">
          <div
            className="inline-flex items-center justify-center w-12 h-12 rounded-2xl mb-4 border"
            style={{
              backgroundColor: "var(--bg-surface)",
              borderColor: "var(--border-hairline)",
              color: "var(--accent)",
            }}
          >
            <BookOpen className="w-6 h-6" />
          </div>
          <h1 className="font-editorial text-4xl font-semibold tracking-tight mb-2">InkPress</h1>
          <p className="text-sm" style={{ color: "var(--fg-secondary)" }}>
            {initialized
              ? "纸墨阅读与 Kindle 出版工作室"
              : "欢迎使用 InkPress · 首次启动请创建管理员账号"}
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border p-7 space-y-4"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-hairline)",
            boxShadow: "var(--shadow-paper)",
          }}
        >
          <div>
            <label
              className="block text-xs font-medium mb-1.5"
              style={{ color: "var(--fg-secondary)" }}
            >
              用户名
            </label>
            <input
              type="text"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg border px-3.5 py-2.5 text-sm focus:outline-none"
              style={{
                backgroundColor: "var(--bg-canvas)",
                borderColor: "var(--border-hairline)",
              }}
            />
          </div>

          <div>
            <label
              className="block text-xs font-medium mb-1.5"
              style={{ color: "var(--fg-secondary)" }}
            >
              密码 {!initialized && <span style={{ color: "var(--fg-muted)" }}>（至少 8 位）</span>}
            </label>
            <input
              type="password"
              required
              minLength={initialized ? 1 : 8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border px-3.5 py-2.5 text-sm focus:outline-none"
              style={{
                backgroundColor: "var(--bg-canvas)",
                borderColor: "var(--border-hairline)",
              }}
            />
          </div>

          {!initialized && (
            <>
              <div>
                <label
                  className="block text-xs font-medium mb-1.5"
                  style={{ color: "var(--fg-secondary)" }}
                >
                  阅读昵称
                </label>
                <input
                  type="text"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  className="w-full rounded-lg border px-3.5 py-2.5 text-sm focus:outline-none"
                  style={{
                    backgroundColor: "var(--bg-canvas)",
                    borderColor: "var(--border-hairline)",
                  }}
                />
              </div>
              <div>
                <label
                  className="block text-xs font-medium mb-1.5"
                  style={{ color: "var(--fg-secondary)" }}
                >
                  Kindle 收件邮箱{" "}
                  <span style={{ color: "var(--fg-muted)" }}>（可选，支持逗号分隔多设备）</span>
                </label>
                <input
                  type="text"
                  placeholder="your_device@kindle.com"
                  value={kindleEmail}
                  onChange={(e) => setKindleEmail(e.target.value)}
                  className="w-full rounded-lg border px-3.5 py-2.5 text-sm focus:outline-none"
                  style={{
                    backgroundColor: "var(--bg-canvas)",
                    borderColor: "var(--border-hairline)",
                  }}
                />
              </div>
            </>
          )}

          {error && (
            <div
              className="text-xs rounded-lg px-3 py-2 border"
              style={{
                backgroundColor: "var(--bg-danger-soft)",
                color: "var(--fg-danger)",
                borderColor: "var(--border-danger)",
              }}
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 rounded-lg py-2.5 text-sm font-medium cursor-pointer transition-opacity disabled:opacity-50"
            style={{
              backgroundColor: "var(--accent)",
              color: "var(--accent-fg)",
            }}
          >
            <span>{loading ? "请稍候…" : initialized ? "进入阅览室" : "完成初始化并进入"}</span>
            <ArrowRight className="w-4 h-4" />
          </button>
        </form>
      </main>

      <footer className="text-center text-xs" style={{ color: "var(--fg-muted)" }}>
        InkPress · Curated News, Books &amp; Magazines for Paper &amp; E-Ink
      </footer>
    </div>
  );
}
