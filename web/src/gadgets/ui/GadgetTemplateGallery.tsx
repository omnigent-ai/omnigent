// The gadget templates as cards. Templates are previewed before use.
import { useId, useMemo, useState } from "react";
import {
  ChartLineIcon,
  FilePlusIcon,
  HistoryIcon,
  Loader2Icon,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useInertRef } from "@/hooks/useInertRef";
import { cn } from "@/lib/utils";
import {
  type Gadget,
  type GadgetContent,
  createGadget,
  createGadgetRevision,
  deleteGadget,
  publishGadgetRevision,
} from "@/gadgets/api/gadgetApi";
import { GadgetFrame } from "@/gadgets/host/GadgetFrame";
import { GADGET_SDK_VERSION } from "@/gadgets/sdk/gadgetProtocol";
import {
  type GadgetTemplate,
  GADGET_TEMPLATES,
  rememberGadgetTemplate,
} from "@/gadgets/templates/gadgetTemplates";
import { GadgetErrorNotice } from "./GadgetErrorNotice";

/** The longest name the server takes. */
export const MAX_GADGET_NAME_LENGTH = 120;

/** The blank template's name until its first build names it. */
const UNTITLED_GADGET = "Untitled gadget";

const TEMPLATE_ICONS: Record<GadgetTemplate["id"], LucideIcon> = {
  "where-was-i": HistoryIcon,
  "data-story": ChartLineIcon,
  blank: FilePlusIcon,
};

// Thumbnails render the template at four times the card's size, then scale it down.
const THUMBNAIL_HEIGHT = 120;
const THUMBNAIL_SCALE = 4;

/** A template running in the frame with in-memory sample data; nothing is saved. */
function TemplatePreview({ template, height }: { template: GadgetTemplate; height: number }) {
  const { gadget, content } = useMemo(() => {
    const previewGadget: Gadget = {
      id: `preview-${template.id}`,
      name: template.name,
      owner_id: "",
      viewer_id: "",
      published_revision: "preview",
      version: null,
      permissions: { read: true, write_data: true, publish: false, manage: false },
    };
    const previewContent: GadgetContent = {
      revision_id: "preview",
      html: template.revision?.html ?? "",
      sdk_version: GADGET_SDK_VERSION,
      capabilities: template.revision?.capabilities ?? [],
    };
    return { gadget: previewGadget, content: previewContent };
  }, [template]);
  if (!template.revision) return null;
  return (
    <div className="flex min-h-0" style={{ height }}>
      <GadgetFrame app={gadget} content={content} onRevisionChanged={() => undefined} preview />
    </div>
  );
}

/** The template itself, small and inert: a card shows what the gadget looks like. */
function TemplateThumbnail({ template }: { template: GadgetTemplate }) {
  // Keeps the frame out of the tab order; the card is the control.
  const inertRef = useInertRef<HTMLDivElement>(true);
  if (!template.revision) {
    const Icon = TEMPLATE_ICONS[template.id];
    return (
      <div
        aria-hidden
        className="flex items-center justify-center rounded-xl border border-border bg-muted text-muted-foreground"
        style={{ height: THUMBNAIL_HEIGHT }}
      >
        <Icon className="size-8" />
      </div>
    );
  }
  return (
    <div
      aria-hidden
      ref={inertRef}
      className="pointer-events-none relative overflow-hidden rounded-xl border border-border bg-background"
      style={{ height: THUMBNAIL_HEIGHT }}
    >
      <div
        className="absolute top-0 left-0 origin-top-left"
        style={{ width: `${THUMBNAIL_SCALE * 100}%`, transform: `scale(${1 / THUMBNAIL_SCALE})` }}
      >
        <TemplatePreview template={template} height={THUMBNAIL_HEIGHT * THUMBNAIL_SCALE} />
      </div>
    </div>
  );
}

/** The steps a create from a template has finished. */
interface CreateProgress {
  templateId: GadgetTemplate["id"];
  gadget: Gadget;
  /** The template's page, once uploaded. */
  revisionId?: string;
  /** Whether the gadget is complete (published, for a template with a page). */
  done: boolean;
}

export interface GadgetTemplateGalleryProps {
  /** Runs once the gadget exists (and a template with a page has published it). */
  onCreated: (gadget: Gadget, template: GadgetTemplate) => void | Promise<void>;
  /** Narrowest card; the grid fits as many columns as the width allows. */
  minCardWidth?: number;
  /** Leaves out the blank template, where a description box beside the gallery starts one. */
  withoutBlank?: boolean;
}

export function GadgetTemplateGallery({
  onCreated,
  minCardWidth = 200,
  withoutBlank = false,
}: GadgetTemplateGalleryProps) {
  const [previewing, setPreviewing] = useState<GadgetTemplate | null>(null);
  const [creating, setCreating] = useState<GadgetTemplate["id"] | null>(null);
  const [failed, setFailed] = useState<{ template: GadgetTemplate; error: unknown } | null>(null);
  // How far a failed create got, so trying again continues from the failed step instead of
  // making another gadget, and giving up deletes the half-made one.
  const [progress, setProgress] = useState<CreateProgress | null>(null);

  const dropHalfMade = () => {
    if (progress !== null && !progress.done) void deleteGadget(progress.gadget.id).catch(() => {});
    setProgress(null);
    setFailed(null);
  };

  const closePreview = () => {
    if (creating !== null) return;
    if (failed !== null && failed.template.id === previewing?.id) dropHalfMade();
    setPreviewing(null);
  };

  // A template's own name, or "Untitled gadget" for Blank; the first build names it.
  const createFromTemplate = async (template: GadgetTemplate) => {
    if (creating !== null) return;
    setCreating(template.id);
    setFailed(null);
    let step: CreateProgress | null = progress?.templateId === template.id ? progress : null;
    if (step === null && progress !== null && !progress.done) {
      void deleteGadget(progress.gadget.id).catch(() => {});
    }
    try {
      if (step === null) {
        const name = template.revision ? template.name : UNTITLED_GADGET;
        step = { templateId: template.id, gadget: await createGadget(name), done: false };
        setProgress(step);
      }
      const { gadget } = step;
      if (template.revision) {
        const revisionId =
          step.revisionId ?? (await createGadgetRevision(gadget.id, template.revision)).id;
        step = { ...step, revisionId };
        setProgress(step);
        await publishGadgetRevision(gadget.id, revisionId, gadget.version);
      }
      step = { ...step, done: true };
      setProgress(step);
      rememberGadgetTemplate(gadget.id, template.id);
      await onCreated(gadget, template);
      setProgress(null);
      setPreviewing(null);
    } catch (error) {
      setFailed({ template, error });
    } finally {
      setCreating(null);
    }
  };

  const templates = GADGET_TEMPLATES.filter((template) => !withoutBlank || template.revision);
  // A failure from a card (Blank) shows under the gallery; one from a preview shows in it.
  const galleryFailure = failed !== null && failed.template.id !== previewing?.id ? failed : null;
  const previewFailure = failed !== null && failed.template.id === previewing?.id ? failed : null;
  return (
    <>
      <ul
        className="m-0 grid list-none gap-3 p-0"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${minCardWidth}px, 1fr))` }}
      >
        {templates.map((template) => (
          <li
            key={template.id}
            aria-busy={creating === template.id || undefined}
            className={cn(
              "relative flex flex-col gap-2 rounded-[20px] border border-border bg-card p-3 transition-colors",
              "hover:border-foreground/20 hover:bg-muted/50",
              "has-[button:focus-visible]:ring-3 has-[button:focus-visible]:ring-ring/50",
              creating === template.id && "opacity-70",
            )}
          >
            <TemplateThumbnail template={template} />
            <button
              type="button"
              aria-label={template.name}
              aria-describedby={`gadget-template-${template.id}-description`}
              disabled={creating !== null}
              onClick={() =>
                template.revision ? setPreviewing(template) : void createFromTemplate(template)
              }
              // The button covers the whole card.
              className="flex cursor-pointer items-center gap-2 text-left text-ui font-medium outline-none after:absolute after:inset-0 after:content-[''] disabled:cursor-default"
            >
              {template.name}
              {creating === template.id && (
                <Loader2Icon className="size-3.5 animate-spin text-muted-foreground" aria-hidden />
              )}
            </button>
            <span
              id={`gadget-template-${template.id}-description`}
              className="text-sm text-muted-foreground"
            >
              {template.description}
            </span>
          </li>
        ))}
      </ul>
      {galleryFailure && (
        <GadgetErrorNotice
          title="Couldn’t create the gadget"
          error={galleryFailure.error}
          onRetry={() => void createFromTemplate(galleryFailure.template)}
        />
      )}

      <Dialog open={previewing !== null} onOpenChange={(open) => !open && closePreview()}>
        <DialogContent className="flex max-h-[90vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-[960px]">
          {previewing && (
            <>
              <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
                <DialogTitle>{previewing.name}</DialogTitle>
                <DialogDescription>{previewing.description}</DialogDescription>
              </DialogHeader>
              <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-6 py-4">
                {/* Phones put the live preview first, where it gets the room. */}
                <div className="flex flex-col gap-1.5 max-sm:order-last">
                  <span className="text-sm font-medium">What it shows</span>
                  <ul
                    className="m-0 flex list-none flex-wrap gap-1.5 p-0"
                    aria-label="What it shows"
                  >
                    {previewing.shows.map((item) => (
                      <li
                        key={item}
                        className="rounded-full border border-border bg-muted/50 px-2.5 py-0.5 text-sm text-muted-foreground"
                      >
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="overflow-hidden rounded-xl border border-border">
                  <TemplatePreview template={previewing} height={480} />
                </div>
                <p className="text-sm text-muted-foreground max-sm:order-1">
                  This preview runs with sample data. Nothing you change here is saved.
                </p>
              </div>
              <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t-0 bg-transparent px-6 py-4">
                {previewFailure && (
                  <GadgetErrorNotice
                    title="Couldn’t create the gadget"
                    error={previewFailure.error}
                    className="mr-auto"
                  />
                )}
                <Button variant="outline" disabled={creating !== null} onClick={closePreview}>
                  Close
                </Button>
                <Button
                  componentId="gadgets.template.use"
                  loading={creating === previewing.id}
                  onClick={() => void createFromTemplate(previewing)}
                >
                  {previewFailure ? "Try again" : "Use template"}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** The gallery under an "Or start from a template" divider. */
export function GadgetTemplatesSection(props: GadgetTemplateGalleryProps) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-3">
      <div className="flex items-center gap-3 before:flex-1 before:border-t before:border-border before:content-[''] after:flex-1 after:border-t after:border-border after:content-['']">
        <h2 id={headingId} className="text-sm font-medium text-muted-foreground">
          Or start from a template
        </h2>
      </div>
      <GadgetTemplateGallery {...props} />
    </section>
  );
}
