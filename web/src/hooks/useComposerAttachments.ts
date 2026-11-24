import { useCallback, useEffect, useRef, useState } from "react";
import {
  attachmentAccept,
  attachmentFilename,
  clipboardFiles,
  validateAttachments,
} from "@/lib/attachments";
import { nameAttachmentFile } from "@/lib/composerMentions";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { insertPastedText } from "@/lib/composerText";
import { largePasteAsFile } from "@/lib/pastedAttachments";
import { isComposerTextarea } from "@/components/composer/engine/composerInput";
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
  onPasteChips?: (files: File[], target: ComposerTextTarget, trayFiles: File[]) => File[];
  /**
   * The pasted-chip tokens, live and released. A picked file that clashes with one is renamed, so
   * its name stays the chip's own; other tray files keep the names they were given.
   */
  chipTokens?: () => readonly string[];
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
  /** Append files a composer's chips leave behind, verbatim: they were validated when pasted. */
  appendFiles: (files: File[]) => void;
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
  chipTokens,
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
  const chipTokensRef = useRef(chipTokens);
  chipTokensRef.current = chipTokens;
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

  // Also sets or clears attachmentError.
  const validateIncoming = useCallback((incoming: File[]): File[] => {
    const { accepted, errors } = validateAttachments(incoming, policyRef.current);
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
    return accepted;
  }, []);

  // A picked file keeps its name unless a pasted chip already uses it, in which case it is the file
  // renamed. Files that clash only with each other keep their names, as they do with no chips.
  const nameIncoming = useCallback((incoming: File[]): File[] => {
    const tokens = chipTokensRef.current?.() ?? [];
    if (tokens.length === 0) return incoming;
    return incoming.map((file) => nameAttachmentFile(file, { tokens, trayNames: [] }));
  }, []);

  const addFiles = useCallback(
    (incoming: File[]) => {
      const accepted = validateIncoming(incoming);
      if (accepted.length === 0) return;
      const named = nameIncoming(accepted);
      setFiles((prev) => [...prev, ...named]);
      onAcceptedRef.current?.(named);
    },
    [validateIncoming, nameIncoming],
  );

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachmentError(null);
    onRemovedRef.current?.();
  }, []);

  const replaceFiles = useCallback(
    (incoming: File[]) => setFiles(nameIncoming(validateIncoming(incoming))),
    [validateIncoming, nameIncoming],
  );

  const restoreFiles = useCallback((restored: File[]) => {
    setFiles(restored);
  }, []);

  // Keep a File once: a restored draft and a reset may both report it.
  const appendFiles = useCallback((demoted: File[]) => {
    setFiles((prev) => [...prev, ...demoted.filter((file) => !prev.includes(file))]);
  }, []);

  const onPaste = useCallback(
    (e: ComposerPasteEvent) => {
      // Chips need the editor that is mounted: a paste into a textarea still rendered during a
      // handoff takes the tray path, as the textarea always does.
      const insertChips = isComposerTextarea(e.currentTarget) ? undefined : onPasteChipsRef.current;
      const pasted = clipboardFiles(e.clipboardData);
      if (pasted.length === 0) {
        // A large text the file policy rejects stays inline, so the text is never lost.
        // A large paste the tray takes must not repeat a file name already there.
        const largeText = insertChips
          ? largePasteAsFile(e.clipboardData, filesRef.current.map(attachmentFilename))
          : null;
        if (
          insertChips === undefined ||
          largeText === null ||
          validateAttachments([largeText], policyRef.current).errors.length > 0
        ) {
          insertPastedText(e);
          return;
        }
        e.preventDefault();
        setAttachmentError(null);
        // A refused insert keeps the text in the tray, so a paste never loses content.
        const refused = insertChips([largeText], e.currentTarget, filesRef.current);
        if (refused.length > 0) addFiles(refused);
        return;
      }
      e.preventDefault();
      if (insertChips) {
        // Insert first: the insert is a text change, which clears the error. Publish this batch's
        // rejections after it.
        const { accepted, errors } = validateAttachments(pasted, policyRef.current);
        const refused =
          accepted.length > 0 ? insertChips(accepted, e.currentTarget, filesRef.current) : [];
        if (refused.length > 0) addFiles(refused);
        setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
        return;
      }
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
    appendFiles,
    onPaste,
    clearError,
    clear,
  };
}
