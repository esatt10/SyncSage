import { useCallback, useState } from "react";

const KEY = "syncsage.assistant.session";

/**
 * The opaque handle for a chat key the user pasted.
 *
 * Only the handle is stored, and only in `sessionStorage` — it dies with the
 * tab, and the key it refers to lives in the server's memory, not here. Nothing
 * recoverable is written to `localStorage` or to disk on either side.
 */
export function useSessionId(): [string | null, (id: string | null) => void] {
  const [sessionId, setSessionIdState] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(KEY);
    } catch {
      return null;
    }
  });

  const setSessionId = useCallback((id: string | null) => {
    setSessionIdState(id);
    try {
      if (id) sessionStorage.setItem(KEY, id);
      else sessionStorage.removeItem(KEY);
    } catch {
      /* session storage may be unavailable */
    }
  }, []);

  return [sessionId, setSessionId];
}
