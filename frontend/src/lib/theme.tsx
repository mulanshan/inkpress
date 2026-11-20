import React, { createContext, useContext, useEffect, useState } from "react";
import type { ReadingFont, ReadingTheme } from "./types";

interface ReaderAppearance {
  theme: ReadingTheme;
  fontFamily: ReadingFont;
  fontSize: number;
  lineHeight: number;
  setTheme: (t: ReadingTheme) => void;
  setFontFamily: (f: ReadingFont) => void;
  setFontSize: (s: number) => void;
  setLineHeight: (lh: number) => void;
}

const STORAGE_KEY = "inkpress_reader_appearance_v1";

const ThemeContext = createContext<ReaderAppearance | null>(null);

function loadStored(): {
  theme: ReadingTheme;
  fontFamily: ReadingFont;
  fontSize: number;
  lineHeight: number;
} {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ReaderAppearance>;
      return {
        theme: parsed.theme ?? "paper",
        fontFamily: parsed.fontFamily ?? "serif",
        fontSize: parsed.fontSize ?? 18,
        lineHeight: parsed.lineHeight ?? 1.85,
      };
    }
  } catch {
    // ignore storage errors
  }
  return { theme: "paper", fontFamily: "serif", fontSize: 18, lineHeight: 1.85 };
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const initial = loadStored();
  const [theme, setTheme] = useState<ReadingTheme>(initial.theme);
  const [fontFamily, setFontFamily] = useState<ReadingFont>(initial.fontFamily);
  const [fontSize, setFontSize] = useState<number>(initial.fontSize);
  const [lineHeight, setLineHeight] = useState<number>(initial.lineHeight);

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
