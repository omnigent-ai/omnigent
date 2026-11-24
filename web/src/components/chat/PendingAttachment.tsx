import { FileTextIcon, ImageIcon, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { formatFileSize } from "@/components/ComposerAttachments";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { uploadProgressKey, useUploadProgress, type UploadProgress } from "@/store/uploadProgress";
import type { UploadFailure } from "@/store/types";

/**
 * A sent message's attachment that hasn't posted yet: its upload's bar and Cancel while it
 * uploads, or the failure with Retry and Remove. Otherwise `fallback`, the attachment as it renders
 * once its upload has ended.
 */
export function PendingAttachment({
  messageId,
  fileId,
  name,
  image = false,
  failure,
  fallback,
}: {
  messageId: string;
  fileId: string;
  name: string;
  image?: boolean;
  failure?: UploadFailure;
  fallback: ReactNode;
}) {
  const upload = useUploadProgress(uploadProgressKey(messageId, fileId));
  if (failure !== undefined && failure.fileId === fileId) {
    return <FailedAttachment name={name} failure={failure} image={image} />;
  }
  if (upload !== undefined)
    return <UploadingAttachment name={name} upload={upload} image={image} />;
  return fallback;
}

function attachmentIcon(image: boolean): LucideIcon {
  return image ? ImageIcon : FileTextIcon;
}

function UploadingAttachment({
  name,
  upload,
  image,
}: {
  name: string;
  upload: UploadProgress;
  image: boolean;
}) {
  const Icon = attachmentIcon(image);
  // One known total drives both the bar and the label, so they cannot disagree.
  const total = upload.total !== null && upload.total > 0 ? upload.total : null;
  const percent = total === null ? null : Math.min(100, Math.floor((upload.loaded / total) * 100));
  const sizeLabel =
    total === null
      ? formatFileSize(upload.size)
      : `${formatFileSize(Math.min(upload.loaded, total))} of ${formatFileSize(total)}`;
  return (
    <div
      data-testid="uploading-attachment"
      className="flex w-60 max-w-full flex-col gap-1.5 rounded-lg border border-border bg-muted px-2.5 py-2 text-sm text-muted-foreground"
    >
      <div className="flex min-w-0 items-center gap-1.5">
        <Icon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate" title={name}>
          {name}
        </span>
        {upload.cancel && (
          <Button
            variant="link"
            size="sm"
            className="h-auto shrink-0 p-0 text-xs"
            aria-label={`Cancel upload of ${name}`}
            onClick={upload.cancel}
          >
            Cancel
          </Button>
        )}
      </div>
      {percent === null ? (
        // Indeterminate: an embedding's transport reports no progress.
        <div
          role="progressbar"
          aria-label={`Uploading ${name}`}
          aria-valuetext="Uploading"
          className="h-1 w-full animate-pulse rounded-full bg-primary/60"
        />
      ) : (
        <Progress value={percent} aria-label={`Uploading ${name}`} aria-valuetext={`${percent}%`} />
      )}
      <span className="text-xs tabular-nums">{sizeLabel}</span>
    </div>
  );
}

export function FailedAttachment({
  name,
  failure,
  image,
}: {
  name: string;
  failure: UploadFailure;
  image: boolean;
}) {
  const Icon = attachmentIcon(image);
  return (
    <div
      data-testid="failed-attachment"
      className="flex w-60 max-w-full flex-col gap-1.5 rounded-lg border border-destructive/40 bg-muted px-2.5 py-2 text-sm text-muted-foreground"
    >
      <div className="flex min-w-0 items-center gap-1.5">
        <Icon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate" title={name}>
          {name}
        </span>
        <Button
          variant="link"
          size="sm"
          className="h-auto shrink-0 p-0 text-xs"
          aria-label={`Retry upload of ${name}`}
          onClick={failure.retry}
        >
          Retry
        </Button>
        <Button
          variant="link"
          size="sm"
          className="h-auto shrink-0 p-0 text-xs"
          aria-label={`Remove failed upload of ${name}`}
          onClick={failure.remove}
        >
          Remove
        </Button>
      </div>
      <span className="text-xs text-destructive">{failure.message}</span>
    </div>
  );
}
