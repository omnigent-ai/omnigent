import { useCallback, useEffect, useRef, useState } from "react";
import { attachmentAccept, clipboardFiles, validateAttachments } from "@/lib/attachments";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { insertPastedText } from "@/lib/composerText";

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

  const addFiles = useCallback((incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming, policyRef.current);
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
    const { accepted, errors } = validateAttachments(incoming, policyRef.current);
    setFiles(accepted);
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  }, []);

  const restoreFiles = useCallback((restored: File[]) => {
    setFiles(restored);
  }, []);

  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const pasted = clipboardFiles(e.clipboardData);
      if (pasted.length === 0) {
        insertPastedText(e);
        return;
      }
      e.preventDefault();
      addFiles(pasted);
    },
    [addFiles],
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
