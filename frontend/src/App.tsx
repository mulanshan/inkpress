import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookMarked,
  BookOpen,
  Compass,
  LogOut,
  Newspaper,
  Palette,
  RefreshCw,
  SlidersHorizontal,
  WifiOff,
} from "lucide-react";
import { ApiError, apiRequest } from "./lib/api";
import { useReaderAppearance } from "./lib/theme";
import type { ReadingTheme, User } from "./lib/types";
import { ReaderSettingsBar } from "./components/ReaderSettingsBar";
import { AuthPage } from "./pages/AuthPage";
import { TodayPage } from "./pages/TodayPage";
import { LibraryPage } from "./pages/LibraryPage";
import { SourcesPage } from "./pages/SourcesPage";
import { StudioPage } from "./pages/StudioPage";

type Space = "today" | "library" | "sources" | "studio";

const VALID_SPACES: Space[] = ["today", "library", "sources", "studio"];
const THEME_LABELS: Record<ReadingTheme, string> = {
  paper: "纸白",
  sepia: "护眼",
  charcoal: "暗夜",
  eink: "墨水屏",
};

function parseHashSpace(): Space {
  const raw = window.location.hash.replace(/^#\/?/, "").toLowerCase();
  return VALID_SPACES.includes(raw as Space) ? (raw as Space) : "today";
}

export function App() {
  const qc = useQueryClient();
  const { theme, cycleTheme, bindUserPreferences } = useReaderAppearance();
  const [space, setSpace] = useState<Space>(() => parseHashSpace());

  const navigateSpace = useCallback((next: Space) => {
    setSpace(next);
    const targetHash = `#${next}`;
    if (window.location.hash !== targetHash) {
      window.location.hash = targetHash;
    }
  }, []);

  useEffect(() => {
    if (!window.location.hash) {
      window.history.replaceState(null, "", `#${space}`);
    }
    const onHashChange = () => {
      setSpace(parseHashSpace());
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const statusQuery = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => apiRequest<{ initialized: boolean }>("/api/v1/auth/status"),
    retry: false,
  });

  const meQuery = useQuery({
    queryKey: ["auth-me"],
    queryFn: () => apiRequest<{ user: User }>("/api/v1/auth/me"),
    enabled: statusQuery.data?.initialized === true,
    retry: false,
  });

  const user = meQuery.data?.user ?? null;

  useEffect(() => {
    bindUserPreferences(user);
  }, [user, bindUserPreferences]);

  if (statusQuery.isLoading || (statusQuery.data?.initialized && meQuery.isLoading)) {
    return (
      <div
        className="min-h-screen flex items-center justify-center text-sm"
        style={{ color: "var(--fg-muted)" }}
      >
        InkPress 载入中…
      </div>
    );
  }

  const isMeServerError =
    meQuery.isError &&
    !(meQuery.error instanceof ApiError && (meQuery.error.status === 401 || meQuery.error.status === 403));

  if (statusQuery.isError || isMeServerError) {
    const errMsg =
      (statusQuery.error instanceof Error && statusQuery.error.message) ||
      (meQuery.error instanceof Error && meQuery.error.message) ||
      "服务暂时不可用";
    return (
      <div className="min-h-screen flex items-center justify-center px-4 py-12">
        <div
          className="max-w-md w-full rounded-2xl border p-8 text-center space-y-4"
          style={{
            backgroundColor: "var(--bg-surface)",
            borderColor: "var(--border-hairline)",
            boxShadow: "var(--shadow-paper)",
          }}
        >
          <div
            className="inline-flex items-center justify-center w-12 h-12 rounded-2xl border mx-auto"
            style={{
              backgroundColor: "var(--bg-subtle)",
              borderColor: "var(--border-hairline)",
              color: "var(--accent)",
            }}
          >
            <WifiOff className="w-6 h-6" />
          </div>
          <div>
            <h1 className="font-editorial text-2xl font-semibold mb-1.5">无法连接服务</h1>
            <p className="text-sm leading-relaxed" style={{ color: "var(--fg-secondary)" }}>
              InkPress 后端可能正在启动、重启或网络暂时中断，请稍候重试。
            </p>
          </div>
          <div
            className="text-xs rounded-lg px-3 py-2 border break-all"
            style={{
              backgroundColor: "var(--bg-danger-soft)",
              color: "var(--fg-danger)",
              borderColor: "var(--border-danger)",
            }}
          >
            {errMsg}
          </div>
          <button
            type="button"
            onClick={() => {
              statusQuery.refetch();
              if (statusQuery.data?.initialized) meQuery.refetch();
            }}
            className="inline-flex items-center justify-center gap-2 rounded-lg px-5 py-2.5 text-sm font-medium cursor-pointer"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
          >
            <RefreshCw className="w-4 h-4" />
            <span>重试连接</span>
          </button>
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <AuthPage
        initialized={Boolean(statusQuery.data?.initialized)}
        onSuccess={() => {
          qc.invalidateQueries({ queryKey: ["auth-status"] });
          qc.invalidateQueries({ queryKey: ["auth-me"] });
        }}
      />
    );
  }

  const handleLogout = async () => {
    try {
      await apiRequest("/api/v1/auth/logout", { method: "POST" });
    } catch {
      // clear local state even if request fails
    }
    bindUserPreferences(null);
    qc.clear();
    qc.invalidateQueries({ queryKey: ["auth-status"] });
  };

  const navItems: { id: Space; label: string; icon: typeof Newspaper }[] = [
    { id: "today", label: "今日刊", icon: Newspaper },
    { id: "library", label: "书架杂志", icon: BookMarked },
    { id: "sources", label: "订阅剪藏", icon: Compass },
    { id: "studio", label: "发行工作室", icon: SlidersHorizontal },
  ];

  return (
    <div className="min-h-screen flex flex-col md:flex-row">
      {/* Mobile Top Header Bar */}
      <header
        className="md:hidden sticky top-0 z-30 border-b px-4 py-2.5 flex items-center justify-between"
        style={{
          backgroundColor: "var(--bg-surface)",
          borderColor: "var(--border-hairline)",
        }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <BookOpen className="w-5 h-5 shrink-0" style={{ color: "var(--accent)" }} />
          <span className="font-editorial text-xl font-semibold tracking-tight truncate">
            InkPress
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={cycleTheme}
            className="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs cursor-pointer"
            style={{
              backgroundColor: "var(--bg-canvas)",
              borderColor: "var(--border-hairline)",
              color: "var(--fg-secondary)",
            }}
            title="切换阅读主题"
          >
            <Palette className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />
            <span>{THEME_LABELS[theme]}</span>
          </button>
          <button
            type="button"
            onClick={handleLogout}
            className="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs cursor-pointer"
            style={{
              backgroundColor: "var(--bg-canvas)",
              borderColor: "var(--border-hairline)",
              color: "var(--fg-muted)",
            }}
            title="退出登录"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span>退出</span>
          </button>
        </div>
      </header>

      {/* Desktop Serene Left Navigation Rail */}
      <aside
        className="hidden md:flex md:w-60 shrink-0 flex-col justify-between border-r px-5 py-7 sticky top-0 h-screen"
        style={{
          backgroundColor: "var(--bg-surface)",
          borderColor: "var(--border-hairline)",
        }}
      >
        <div>
          <div className="flex items-center gap-2.5 mb-8 px-1">
            <BookOpen className="w-6 h-6" style={{ color: "var(--accent)" }} />
            <div>
              <div className="font-editorial text-2xl font-semibold leading-none tracking-tight">
                InkPress
              </div>
              <div className="text-[11px] mt-0.5" style={{ color: "var(--fg-muted)" }}>
                纸墨阅览与发行
              </div>
            </div>
          </div>

          <nav className="space-y-1.5">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = space === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => navigateSpace(item.id)}
                  className="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium cursor-pointer transition-colors"
                  style={{
                    backgroundColor: active ? "var(--accent-soft)" : "transparent",
                    color: active ? "var(--accent)" : "var(--fg-secondary)",
                  }}
                >
                  <Icon className="w-4 h-4" />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        <div className="space-y-4">
          <ReaderSettingsBar compact />
          <div
            className="flex items-center justify-between pt-3 border-t text-xs"
            style={{ borderColor: "var(--border-hairline)", color: "var(--fg-muted)" }}
          >
            <span className="truncate">{user.display_name}</span>
            <button
              type="button"
              onClick={handleLogout}
              className="inline-flex items-center gap-1 hover:opacity-80 cursor-pointer"
              title="退出登录"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span>退出</span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main Editorial Workspace */}
      <main className="flex-1 px-4 sm:px-8 lg:px-12 py-6 sm:py-9 pb-24 md:pb-10 min-w-0">
        {space === "today" && <TodayPage onGoSources={() => navigateSpace("sources")} />}
        {space === "library" && <LibraryPage />}
        {space === "sources" && <SourcesPage />}
        {space === "studio" && <StudioPage />}
      </main>

      {/* Mobile Bottom Navigation Bar */}
      <nav
        className="md:hidden fixed bottom-0 inset-x-0 z-30 border-t grid grid-cols-4 py-2 px-2"
        style={{
          backgroundColor: "var(--bg-surface)",
          borderColor: "var(--border-hairline)",
        }}
      >
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = space === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => navigateSpace(item.id)}
              className="flex flex-col items-center gap-1 py-1 text-[11px] font-medium cursor-pointer"
              style={{ color: active ? "var(--accent)" : "var(--fg-muted)" }}
            >
              <Icon className="w-4 h-4" />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
