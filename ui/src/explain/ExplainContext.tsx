import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface ExplainState {
  enabled: boolean;
  toggle: () => void;
  panelOpen: boolean;
  setPanelOpen: (open: boolean) => void;
}

const ExplainCtx = createContext<ExplainState | null>(null);

export function ExplainProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const toggle = useCallback(() => setEnabled((value) => !value), []);
  const value = useMemo(
    () => ({ enabled, toggle, panelOpen, setPanelOpen }),
    [enabled, toggle, panelOpen],
  );
  return <ExplainCtx.Provider value={value}>{children}</ExplainCtx.Provider>;
}

export function useExplain(): ExplainState {
  const ctx = useContext(ExplainCtx);
  if (!ctx) throw new Error("useExplain must be used within ExplainProvider");
  return ctx;
}
