"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Whether this browser closed this notice. `key` names the exact notice, so a
 * notice whose key changes (a new message, a new day) shows again.
 */
export function useDismissed(slot: string, key: string): [boolean, () => void] {
  const [closed, setClosed] = useState<string | null>(null);

  useEffect(() => {
    try {
      setClosed(window.localStorage.getItem(`sidra.closed.${slot}`));
    } catch {
      /* without storage the notice simply shows */
    }
  }, [slot]);

  const dismiss = useCallback(() => {
    setClosed(key);
    try {
      window.localStorage.setItem(`sidra.closed.${slot}`, key);
    } catch {
      /* closed until the page reloads */
    }
  }, [slot, key]);

  return [closed === key, dismiss];
}
