import React, { createContext, useCallback, useContext, useState } from "react";

interface ToastItem {
  id: number;
  message: string;
  tone: "info" | "success" | "error";
}

interface ToastContextValue {
  notify: (message: string, tone?: "info" | "success" | "error") => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const notify = useCallback((message: string, tone: "info" | "success" | "error" = "info") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-3), { id, message, tone }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id));
    }, 3600);
  }, []);

  return (
    <ToastContext.Provider value={{ notify }}>
      {children}
      <div className="fixed bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none">
        {items.map((item) => (
          <div
            key={item.id}
            className="pointer-events-auto rounded-lg px-4 py-2.5 text-sm font-medium border max-w-sm"
            style={{
              backgroundColor:
                item.tone === "error"
                  ? "#7f1d1d"
                  : item.tone === "success"
                    ? "var(--fg-primary)"
                    : "var(--bg-surface)",
              color:
                item.tone === "error"
                  ? "#ffffff"
                  : item.tone === "success"
                    ? "var(--bg-canvas)"
                    : "var(--fg-primary)",
              borderColor: "var(--border-hairline)",
              boxShadow: "var(--shadow-paper)",
            }}
          >
            {item.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
