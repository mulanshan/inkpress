import { Minus, Plus, Type } from "lucide-react";
import { useReaderAppearance } from "../lib/theme";
import type { ReadingTheme } from "../lib/types";

const THEMES: { id: ReadingTheme; label: string; bg: string; fg: string }[] = [
  { id: "paper", label: "纸白", bg: "#FAF8F5", fg: "#1C1B18" },
  { id: "sepia", label: "护眼", bg: "#F4ECD8", fg: "#2B2318" },
  { id: "charcoal", label: "暗夜", bg: "#141413", fg: "#E8E3D9" },
  { id: "eink", label: "墨水屏", bg: "#FFFFFF", fg: "#000000" },
];

export function ReaderSettingsBar({ compact = false }: { compact?: boolean }) {
  const { theme, fontFamily, fontSize, setTheme, setFontFamily, setFontSize } =
    useReaderAppearance();

  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-xl border px-3 py-2 text-xs"
      style={{
        backgroundColor: "var(--bg-surface)",
        borderColor: "var(--border-hairline)",
      }}
    >
      <div className="flex items-center gap-1.5">
        {THEMES.map((t) => {
          const active = theme === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setTheme(t.id)}
              title={`切换至${t.label}主题`}
              className="flex items-center gap-1 rounded-full px-2.5 py-1 border cursor-pointer transition-opacity"
              style={{
                backgroundColor: t.bg,
                color: t.fg,
                borderColor: active ? "var(--accent)" : "var(--border-hairline)",
                outline: active ? "1.5px solid var(--accent)" : "none",
              }}
            >
              <span>{t.label}</span>
            </button>
          );
        })}
      </div>

      {!compact && (
        <>
          <span className="h-4 w-px" style={{ backgroundColor: "var(--border-hairline)" }} />
          <div className="flex items-center gap-1">
            <Type className="w-3.5 h-3.5 mr-0.5" style={{ color: "var(--fg-muted)" }} />
            <button
              type="button"
              onClick={() => setFontFamily("serif")}
              className="rounded px-2 py-0.5 cursor-pointer font-editorial"
              style={{
                backgroundColor: fontFamily === "serif" ? "var(--bg-subtle)" : "transparent",
                color: fontFamily === "serif" ? "var(--fg-primary)" : "var(--fg-muted)",
              }}
            >
              宋体/衬线
            </button>
            <button
              type="button"
              onClick={() => setFontFamily("sans")}
              className="rounded px-2 py-0.5 cursor-pointer font-sans-clean"
              style={{
                backgroundColor: fontFamily === "sans" ? "var(--bg-subtle)" : "transparent",
                color: fontFamily === "sans" ? "var(--fg-primary)" : "var(--fg-muted)",
              }}
            >
              黑体/无衬线
            </button>
          </div>

          <span className="h-4 w-px" style={{ backgroundColor: "var(--border-hairline)" }} />
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setFontSize(Math.max(15, fontSize - 1))}
              className="rounded p-1 hover:opacity-80 cursor-pointer"
              style={{ backgroundColor: "var(--bg-subtle)" }}
              aria-label="减小字号"
            >
              <Minus className="w-3.5 h-3.5" />
            </button>
            <span className="tabular-nums w-8 text-center" style={{ color: "var(--fg-secondary)" }}>
              {fontSize}px
            </span>
            <button
              type="button"
              onClick={() => setFontSize(Math.min(24, fontSize + 1))}
              className="rounded p-1 hover:opacity-80 cursor-pointer"
              style={{ backgroundColor: "var(--bg-subtle)" }}
              aria-label="增大字号"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
          </div>
        </>
      )}
    </div>
  );
}
