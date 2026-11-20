import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { apiRequest } from "./api";
import type { ReadingFont, ReadingTheme, User } from "./types";

interface ReaderAppearance {
  theme: ReadingTheme;
  fontFamily: ReadingFont;
  fontSize: number;
  lineHeight: number;
  setTheme: (t: ReadingTheme) => void;
  setFontFamily: (f: ReadingFont) => void;
  setFontSize: (s: number) => void;
  setLineHeight: (lh: number) => void;
  cycleTheme: () => void;
  bindUserPreferences: (user: User | null) => void;
}

const STORAGE_KEY = "inkpress_reader_appearance_v1";
const VALID_THEMES: ReadingTheme[] = ["paper", "sepia", "charcoal", "eink"];
const VALID_FONTS: ReadingFont[] = ["serif", "sans"];

const ThemeContext = createContext<ReaderAppearance | null>(null);

function isValidTheme(v: unknown): v is ReadingTheme {
  return typeof v === "string" && VALID_THEMES.includes(v as ReadingTheme);
}

function isValidFont(v: unknown): v is ReadingFont {
  return typeof v === "string" && VALID_FONTS.includes(v as ReadingFont);
}

function loadStored(): {
  theme: ReadingTheme;
  fontFamily: ReadingFont;
  fontSize: number;
  lineHeight: number;
} {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<{
        theme: unknown;
        fontFamily: unknown;
        fontSize: unknown;
        lineHeight: unknown;
      }>;
      const fs =
        typeof parsed.fontSize === "number" && parsed.fontSize >= 14 && parsed.fontSize <= 26
          ? parsed.fontSize
          : 18;
      const lh =
        typeof parsed.lineHeight === "number" && parsed.lineHeight >= 1.4 && parsed.lineHeight <= 2.4
          ? parsed.lineHeight
          : 1.85;
      return {
        theme: isValidTheme(parsed.theme) ? parsed.theme : "paper",
        fontFamily: isValidFont(parsed.fontFamily) ? parsed.fontFamily : "serif",
        fontSize: fs,
        lineHeight: lh,
      };
    }
  } catch {
    // ignore storage errors
  }
  return { theme: "paper", fontFamily: "serif", fontSize: 18, lineHeight: 1.85 };
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const initial = loadStored();
  const [theme, setThemeState] = useState<ReadingTheme>(initial.theme);
  const [fontFamily, setFontFamilyState] = useState<ReadingFont>(initial.fontFamily);
  const [fontSize, setFontSizeState] = useState<number>(initial.fontSize);
  const [lineHeight, setLineHeightState] = useState<number>(initial.lineHeight);
  const [authenticated, setAuthenticated] = useState(false);

  const hydratedUserIdRef = useRef<number | null>(null);
  const dirtyRef = useRef(false);

  const setTheme = useCallback((t: ReadingTheme) => {
    dirtyRef.current = true;
    setThemeState(t);
  }, []);

  const setFontFamily = useCallback((f: ReadingFont) => {
    dirtyRef.current = true;
    setFontFamilyState(f);
  }, []);

  const setFontSize = useCallback((s: number) => {
    dirtyRef.current = true;
    setFontSizeState(s);
  }, []);

  const setLineHeight = useCallback((lh: number) => {
    dirtyRef.current = true;
    setLineHeightState(lh);
  }, []);

  const cycleTheme = useCallback(() => {
    dirtyRef.current = true;
    setThemeState((prev) => {
      const idx = VALID_THEMES.indexOf(prev);
      return VALID_THEMES[(idx + 1) % VALID_THEMES.length];
    });
  }, []);

  const bindUserPreferences = useCallback((user: User | null) => {
    if (!user) {
      setAuthenticated(false);
      hydratedUserIdRef.current = null;
      dirtyRef.current = false;
      return;
    }
    setAuthenticated(true);
    if (hydratedUserIdRef.current === user.id) return;
    hydratedUserIdRef.current = user.id;
    dirtyRef.current = false;
    const prefs = user.preferences;
    if (!prefs) return;
    if (isValidTheme(prefs.theme)) setThemeState(prefs.theme);
    if (isValidFont(prefs.font_family)) setFontFamilyState(prefs.font_family);
    if (typeof prefs.font_size === "number" && prefs.font_size >= 14 && prefs.font_size <= 26) {
      setFontSizeState(prefs.font_size);
    }
    if (
      typeof prefs.line_height === "number" &&
      prefs.line_height >= 1.4 &&
      prefs.line_height <= 2.4
    ) {
      setLineHeightState(prefs.line_height);
    }
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ theme, fontFamily, fontSize, lineHeight }),
      );
    } catch {
      // ignore
    }
  }, [theme, fontFamily, fontSize, lineHeight]);

  // Debounce-sync appearance changes back to server when authenticated
  useEffect(() => {
    if (!authenticated || !dirtyRef.current) return;
    const timer = window.setTimeout(() => {
      apiRequest("/api/v1/auth/me", {
        method: "PATCH",
        body: JSON.stringify({
          preferences: {
            theme,
            font_family: fontFamily,
            font_size: fontSize,
            line_height: lineHeight,
          },
        }),
      })
        .then(() => {
          dirtyRef.current = false;
        })
        .catch(() => {
          // keep local preference even if offline
        });
    }, 650);
    return () => window.clearTimeout(timer);
  }, [authenticated, theme, fontFamily, fontSize, lineHeight]);

  return (
    <ThemeContext.Provider
      value={{
        theme,
        fontFamily,
        fontSize,
        lineHeight,
        setTheme,
        setFontFamily,
        setFontSize,
        setLineHeight,
        cycleTheme,
        bindUserPreferences,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function useReaderAppearance(): ReaderAppearance {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useReaderAppearance must be used within ThemeProvider");
  return ctx;
}
