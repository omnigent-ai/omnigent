import { useCallback, useRef, useState } from "react";
import { validateAttachments } from "@/lib/attachments";

export interface UseComposerAttachmentsOptions {
  /**
   * Files to start with. Used verbatim — trusted input is not re-validated,
   * so nothing the composer already accepted can be silently dropped.
   */
  initialFiles?: File[];
  /**
   * Fires synchronously inside addFiles when a batch accepts at least one
   * file — before React commits the append, so readers still see the prior
   * list. Receives the accepted files.
   */
  onAccepted?: (accepted: File[]) => void;
  /** Runs after a removal. */
  onRemoved?: () => void;
}

export interface ComposerAttachmentsApi {
  files: File[];
  attachmentError: string | null;
  /** Validate, append the accepted files, and surface the rejections. */
  addFiles: (incoming: File[]) => void;
  /** Drop one file by index and clear the rejection notice. */
  removeFile: (index: number) => void;
  /**
   * Validating wholesale replacement: the list becomes the accepted files
   * and the rejections become the notice. For input of unknown provenance.
   */
  replaceFiles: (incoming: File[]) => void;
  /**
   * Verbatim wholesale replacement for already-trusted files — re-validating
   * could silently drop them. Leaves the notice untouched.
   */
  restoreFiles: (files: File[]) => void;
  /**
   * Attach file-kind clipboard items instead of letting them insert as
   * text. The event is claimed only when a usable file was present, so a
   * plain-text paste keeps its native behavior.
   */
  onPaste: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  clearError: () => void;
  clear: () => void;
}

/**
 * Attachment state for a composer: the accepted files plus the rejection
 * notice from client-side validation (`@/lib/attachments`). Every returned
 * action keeps a stable identity across renders, so the actions are safe
 * in effect dependency lists; option callbacks are always invoked at their
 * latest via refs.
 */
export function useComposerAttachments({
  initialFiles,
  onAccepted,
  onRemoved,
}: UseComposerAttachmentsOptions = {}): ComposerAttachmentsApi {
  const [files, setFiles] = useState<File[]>(() => initialFiles ?? []);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  // Callback freshness is separated from action identity: the refs always
  // hold the latest options, so the actions below never need re-creating.
  const onAcceptedRef = useRef(onAccepted);
  onAcceptedRef.current = onAccepted;
  const onRemovedRef = useRef(onRemoved);
  onRemovedRef.current = onRemoved;

  const addFiles = useCallback((incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming);
    if (accepted.length > 0) {
      setFiles((prev) => [...prev, ...accepted]);
      onAcceptedRef.current?.(accepted);
    }
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  }, []);

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachmentError(null);
    onRemovedRef.current?.();
  }, []);

  const replaceFiles = useCallback((incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming);
    setFiles(accepted);
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  }, []);

  const restoreFiles = useCallback((restored: File[]) => {
    setFiles(restored);
  }, []);

  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
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
    },
    [addFiles],
  );

  const clearError = useCallback(() => setAttachmentError(null), []);

  const clear = useCallback(() => {
    setFiles([]);
    setAttachmentError(null);
  }, []);

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
