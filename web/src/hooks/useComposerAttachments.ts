import { useState } from "react";
import { validateAttachments } from "@/lib/attachments";

export interface UseComposerAttachmentsOptions {
  /** Files already attached when the composer mounts (a restored draft). */
  initialFiles?: File[];
  /**
   * Runs after accepted files append — the caller's side effects (refocus
   * the textarea, mark the draft dirty) live here so the hook never learns
   * about sessions, textareas, or focus.
   */
  onAccepted?: (accepted: File[]) => void;
  /** Runs after a removal, for the same caller-owned side effects. */
  onRemoved?: () => void;
}

export interface ComposerAttachmentsApi {
  files: File[];
  attachmentError: string | null;
  /** Validate, append the accepted files, and surface the rejections. */
  addFiles: (incoming: File[]) => void;
  /** Drop one chip; the stale rejection notice goes with it. */
  removeFile: (index: number) => void;
  /**
   * Validating wholesale replace — a stored draft handed back for editing
   * (a send that never reached the server) is re-checked because it may
   * predate the current limits.
   */
  replaceFiles: (incoming: File[]) => void;
  /**
   * Non-validating wholesale set — files the composer itself accepted
   * earlier (a recalled message, a restored draft) come back verbatim;
   * re-validating could silently drop what the user already attached.
   */
  restoreFiles: (files: File[]) => void;
  /**
   * Pasted images/files attach instead of inserting as text; the event is
   * claimed only when the clipboard actually carried a usable file, so a
   * plain-text paste keeps its native behavior.
   */
  onPaste: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  clearError: () => void;
  clear: () => void;
}

/**
 * Attachment state for a composer: the accepted files plus the rejection
 * notice, with the add/remove/paste affordances both composer surfaces
 * share. Validation runs here, before any upload — a bad file caught only
 * at upload time would surface as a server 415 after the message was
 * already sent. The hook owns no side effects beyond its own state;
 * callers wire theirs (focus, dirty tracking) through the callbacks.
 */
export function useComposerAttachments({
  initialFiles,
  onAccepted,
  onRemoved,
}: UseComposerAttachmentsOptions = {}): ComposerAttachmentsApi {
  const [files, setFiles] = useState<File[]>(() => initialFiles ?? []);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);

  const addFiles = (incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming);
    if (accepted.length > 0) {
      setFiles((prev) => [...prev, ...accepted]);
      onAccepted?.(accepted);
    }
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachmentError(null);
    onRemoved?.();
  };

  const replaceFiles = (incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming);
    setFiles(accepted);
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  };

  const restoreFiles = (restored: File[]) => {
    setFiles(restored);
  };

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const pasted: File[] = [];
    for (const item of items) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile();
      if (file !== null) pasted.push(file);
    }
    if (pasted.length > 0) {
      e.preventDefault();
      addFiles(pasted);
    }
  };

  const clearError = () => setAttachmentError(null);

  const clear = () => {
    setFiles([]);
    setAttachmentError(null);
  };

  return {
    files,
    attachmentError,
    addFiles,
    removeFile,
    replaceFiles,
    restoreFiles,
    onPaste,
    clearError,
    clear,
  };
}
