import { useCallback, useEffect, useState } from "react";

export type Theme = "openseo" | "openseo-dark";

const STORAGE_KEY = "openseo-plus-theme";

const systemPrefersDark = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-color-scheme: dark)").matches;

function resolveInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "openseo" || stored === "openseo-dark") return stored;
  } catch {
    // Storage unavailable (private mode etc.) — fall through to system.
  }
  return systemPrefersDark() ? "openseo-dark" : "openseo";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Ignore storage failures; theme still applies for this session.
    }
  }, [theme]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      try {
        if (!localStorage.getItem(STORAGE_KEY)) {
          setTheme(systemPrefersDark() ? "openseo-dark" : "openseo");
        }
      } catch {
        setTheme(systemPrefersDark() ? "openseo-dark" : "openseo");
      }
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((current) =>
      current === "openseo" ? "openseo-dark" : "openseo",
    );
  }, []);

  return { theme, toggleTheme };
}