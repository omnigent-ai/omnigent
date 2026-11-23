import { useCallback, useEffect, useRef, useState } from "react";
import { attachmentAccept, clipboardFiles, validateAttachments } from "@/lib/attachments";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { insertPastedText } from "@/lib/composerText";
import { largePasteAsFile } from "@/lib/pastedAttachments";
import type {
  ComposerPasteEvent,
  ComposerTextTarget,
} from "@/components/composer/engine/composerInput";

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
  /**
   * Insert pasted files as chips at the paste's caret instead of adding them to the tray. Receives
   * only validated files; a large plain-text paste arrives as one `.txt` file. Without it, pastes
   * attach to the tray as before.
   */
  onPasteChips?: (files: File[], target: ComposerTextTarget, trayFiles: File[]) => void;
}

export interface ComposerAttachmentsApi {
  files: File[];
  attachmentError: string | null;
  /** The file picker's `accept` filter for the server's attachment policy. */
  accept: string | undefined;
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
   * text. Without a usable file, plain text is inserted as typed input
   * (see insertPastedText).
   */
  onPaste: (e: ComposerPasteEvent) => void;
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
  onPasteChips,
}: UseComposerAttachmentsOptions = {}): ComposerAttachmentsApi {
  const [files, setFiles] = useState<File[]>(() => initialFiles ?? []);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  // Callback freshness is separated from action identity: the refs always
  // hold the latest options, so the actions below never need re-creating.
  const onAcceptedRef = useRef(onAccepted);
  onAcceptedRef.current = onAccepted;
  const onRemovedRef = useRef(onRemoved);
  onRemovedRef.current = onRemoved;
  const onPasteChipsRef = useRef(onPasteChips);
  onPasteChipsRef.current = onPasteChips;
  // The server's filesystem attachment policy, read through a ref for the same reason.
  const info = useServerInfo();
  const policy = info === "loading" ? null : (info.filesystem_attachments ?? null);
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const filesRef = useRef(files);
  filesRef.current = files;

  // Files added before /v1/info loaded were checked against the default policy,
  // so recheck them against the server's once it arrives.
  const checkedPolicyRef = useRef(policy);
  useEffect(() => {
    if (checkedPolicyRef.current === policy) return;
    checkedPolicyRef.current = policy;
    const { accepted, errors } = validateAttachments(filesRef.current, policy);
    if (errors.length === 0) return;
    setFiles(accepted);
    setAttachmentError(errors.join("\n"));
    onRemovedRef.current?.();
  }, [policy]);

  // Validate and surface the rejections; returns the files that passed.
  const validateIncoming = useCallback((incoming: File[]): File[] => {
    const { accepted, errors } = validateAttachments(incoming, policyRef.current);
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
    return accepted;
  }, []);

  const addFiles = useCallback(
    (incoming: File[]) => {
      const accepted = validateIncoming(incoming);
      if (accepted.length > 0) {
        setFiles((prev) => [...prev, ...accepted]);
        onAcceptedRef.current?.(accepted);
      }
    },
    [validateIncoming],
  );

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachmentError(null);
    onRemovedRef.current?.();
  }, []);

  const replaceFiles = useCallback(
    (incoming: File[]) => setFiles(validateIncoming(incoming)),
    [validateIncoming],
  );

  const restoreFiles = useCallback((restored: File[]) => {
    setFiles(restored);
  }, []);

  const onPaste = useCallback(
    (e: ComposerPasteEvent) => {
      const insertChips = onPasteChipsRef.current;
      const pasted = clipboardFiles(e.clipboardData);
      if (pasted.length === 0) {
        // A large text the file policy rejects stays inline, so the text is never lost.
        const largeText = insertChips ? largePasteAsFile(e.clipboardData) : null;
        if (
          insertChips === undefined ||
          largeText === null ||
          validateAttachments([largeText], policyRef.current).errors.length > 0
        ) {
          insertPastedText(e);
          return;
        }
        e.preventDefault();
        insertChips([largeText], e.currentTarget, filesRef.current);
        return;
      }
      e.preventDefault();
      if (insertChips) {
        const accepted = validateIncoming(pasted);
        if (accepted.length > 0) insertChips(accepted, e.currentTarget, filesRef.current);
        return;
      }
      addFiles(pasted);
    },
    [addFiles, validateIncoming],
  );

  const clearError = useCallback(() => setAttachmentError(null), []);

  const clear = useCallback(() => {
    setFiles([]);
    setAttachmentError(null);
  }, []);

  const accept = attachmentAccept(policy);

  return {
    files,
    attachmentError,
    accept,
    addFiles,
    removeFile,
    replaceFiles,
    restoreFiles,
    onPaste,
    clearError,
    clear,
  };
}
