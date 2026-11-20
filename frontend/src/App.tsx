import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookMarked,
  BookOpen,
  Compass,
  LogOut,
  Newspaper,
  SlidersHorizontal,
} from "lucide-react";
import { apiRequest } from "./lib/api";
import type { User } from "./lib/types";
import { ReaderSettingsBar } from "./components/ReaderSettingsBar";
import { AuthPage } from "./pages/AuthPage";
import { TodayPage } from "./pages/TodayPage";
import { LibraryPage } from "./pages/LibraryPage";
import { SourcesPage } from "./pages/SourcesPage";
import { StudioPage } from "./pages/StudioPage";

type Space = "today" | "library" | "sources" | "studio";

export function App() {
  const qc = useQueryClient();
  const [space, setSpace] = useState<Space>("today");

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

  if (statusQuery.isLoading || (statusQuery.data?.initialized && meQuery.isLoading)) {
    return (
      <div className="min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--fg-muted)" }}>
        InkPress 载入中…
      </div>
    );
  }

  const user = meQuery.data?.user ?? null;
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
    await apiRequest("/api/v1/auth/logout", { method: "POST" });
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
                  onClick={() => setSpace(item.id)}
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
        {space === "today" && <TodayPage onGoSources={() => setSpace("sources")} />}
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
              onClick={() => setSpace(item.id)}
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
