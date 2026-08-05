import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const KEY = "pheasant.theme";

function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* storage unavailable */
  }
  // Light is the product default; only follow the OS when it explicitly asks
  // for dark, so a machine with no preference set gets the light design.
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * The theme currently applied to `<html>`, for consumers that need concrete
 * colour values instead of CSS custom properties — Cytoscape draws to a
 * `<canvas>` and never resolves `var(--…)`.
 *
 * This observes the attribute `useTheme` writes rather than owning state of its
 * own, so the toggle stays the single source of truth and there is no second
 * copy to keep in sync.
 */
export function useAppliedTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(() =>
    document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
  );

  useEffect(() => {
    const root = document.documentElement;
    const read = () => setTheme(root.getAttribute("data-theme") === "dark" ? "dark" : "light");
    // The attribute is set in an effect, so it may not be there on first paint.
    read();
    const observer = new MutationObserver(read);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  return theme;
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* storage unavailable */
    }
  }, [theme]);

  const toggle = useCallback(
    () => setTheme((current) => (current === "dark" ? "light" : "dark")),
    [],
  );

  return [theme, toggle];
}
