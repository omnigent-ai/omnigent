import { useCallback, useEffect, useState } from "react";
import { copyText } from "@/lib/clipboard";

export type CopyLinkStatus = "idle" | "copied" | "failed";

/** Copies a link; "copied" lasts 2 seconds, "failed" stays until `reset` or the next copy. */
export function useCopyLink() {
  const [status, setStatus] = useState<CopyLinkStatus>("idle");

  useEffect(() => {
    if (status !== "copied") return;
    const id = setTimeout(() => setStatus("idle"), 2000);
    return () => clearTimeout(id);
  }, [status]);

  const copy = useCallback(async (url: string) => {
    try {
      await copyText(url);
      setStatus("copied");
    } catch (error) {
      console.warn("Failed to copy link", error);
      setStatus("failed");
    }
  }, []);

  const reset = useCallback(() => setStatus("idle"), []);
  return { status, copy, reset };
}
