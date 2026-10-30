// Expanded view of a file-edit or write tool call: the change as a diff per file,
// with the tool's reported failure, or output that does not confirm it, above it.

import { type ReactNode, useMemo } from "react";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { type FileDiffSection, getFileEditDiff } from "@/lib/toolEditDiff";
import { type ToolCall, editApplied } from "@/lib/toolResultSummary";
import { editErrorMessage } from "@/lib/toolValue";
import { cn } from "@/lib/utils";
import { useWorkspacePaths } from "@/shell/FileViewerContext";
import { CodePanel, DIFF_TONE_CLASS, OutputSection, ToolErrorBox } from "./ToolCodePanel";
import { ToolValueView } from "./ToolValueView";

const EMPTY_FAILURE = "The edit failed with no output.";

export function FileEditBody({ call }: { call: ToolCall }) {
  const { name, args, output, state } = call;
  // Before output arrives, the card shows the pending or cancelled state below instead.
  const awaitingOutput = output === null && state !== "output-available";
  const diff = useMemo(() => getFileEditDiff(name, args, output), [name, args, output]);
  const error = useMemo(() => {
    if (output === null) return null;
    // Defensive: the transcript sets output-error only without output, but a direct
    // caller's failed output is its error even without a recognized prefix.
    const failure = state === "output-error" ? output.trim() || EMPTY_FAILURE : null;
    return editErrorMessage(output) ?? failure;
  }, [output, state]);
  // The diff is what was requested; show output that does not say it applied.
  const unconfirmed =
    output !== null && error === null && output.trim() !== "" && !editApplied(name, output);
  const outputPanel = output !== null && <OutputSection output={output} language="text" />;
  return (
    <div className="space-y-2">
      {error !== null && <ToolErrorBox>{error}</ToolErrorBox>}
      {diff === null ? (
        <>
          {!awaitingOutput && (
            <p className="text-muted-foreground text-sm">No diff recorded for this edit.</p>
          )}
          {Object.keys(args).length > 0 && <ToolValueView value={args} />}
          {output !== null && error === null && output.trim() !== "" && outputPanel}
        </>
      ) : (
        <>
          {unconfirmed && outputPanel}
          {(error !== null || unconfirmed) && (
            <p className="text-muted-foreground text-sm">
              {error !== null
                ? "Requested change, not applied:"
                : "Requested change; the output does not confirm it applied:"}
            </p>
          )}
          {diff.files.map((file) => (
            // A patch lists each file once, so its path names its panel.
            <FileDiffPanel key={file.path} file={file} perMatch={diff.countsArePerMatch} />
          ))}
        </>
      )}
    </div>
  );
}

/** One file's diff, titled with its path, its kind of change and its counts. */
function FileDiffPanel({ file, perMatch }: { file: FileDiffSection; perMatch: boolean }) {
  const { root, home } = useWorkspacePaths();
  const shown = (path: string) => resolveChatFilePath(path, root, home)?.path ?? path;
  // An edit call that names no file gets an untitled panel.
  const title: ReactNode =
    file.path !== "" ? (
      <>
        {shown(file.path)}
        {file.newPath !== undefined && ` renamed to ${shown(file.newPath)}`}
        {file.kind === "add" && " (new file)"}
        {file.kind === "delete" && " (deleted)"}
        {file.kind === "write" && " (whole file written)"}
        {perMatch && " (each match)"}
        {file.additions > 0 && (
          <span className={cn("ml-2", DIFF_TONE_CLASS.added)}>+{file.additions}</span>
        )}
        {file.removals > 0 && (
          <span className={cn("ml-1", DIFF_TONE_CLASS.removed)}>-{file.removals}</span>
        )}
      </>
    ) : undefined;
  if (file.text === "") {
    // An empty file added, deleted or written, or a rename that changes no lines.
    const what = file.kind === "write" ? "empty file" : "no line changes";
    return (
      <p className="text-muted-foreground text-sm">
        {title ?? shown(file.path)}: {what}
      </p>
    );
  }
  // The previous content of a rewritten file is not recorded, so it shows as written.
  const isWrite = file.kind === "write";
  return (
    <OutputSection
      output={file.text}
      renderPanel={(text) => (
        <CodePanel
          title={title}
          text={text}
          copyText={file.text}
          copyLabel={isWrite ? "Copy file" : "Copy diff"}
          language={isWrite ? "text" : "diff"}
        />
      )}
    />
  );
}
