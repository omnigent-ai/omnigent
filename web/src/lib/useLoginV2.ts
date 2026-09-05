// Sticky flag for the redesigned (v2) auth pages. `?login-v2=1`/`=0` toggles and
// persists; with no param the stored value wins. Persisted because the accounts
// 401→/login redirect drops the query string, so a param-only flag wouldn't stick.

import { useSearchParams } from "@/lib/routing";

const STORAGE_KEY = "omnigent.loginV2";

function store(enabled: boolean): void {
  try {
    if (enabled) window.localStorage.setItem(STORAGE_KEY, "1");
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // localStorage can throw in sandboxed/blocked-cookies contexts.
  }
}

export function useLoginV2(): boolean {
  const raw = useSearchParams()[0].get("login-v2");
  if (raw === "1" || raw === "0") {
    const on = raw === "1";
    store(on);
    return on;
  }
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}
