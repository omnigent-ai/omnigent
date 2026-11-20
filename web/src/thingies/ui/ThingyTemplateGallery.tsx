// The thingy templates as cards. A template with a page is previewed first; each is then named
// before it is created. The New thingy dialog, the create page and a session's New thingy tab
// offer them.
import { type FormEvent, useId, useMemo, useState } from "react";
import { ChartLineIcon, FilePlusIcon, HistoryIcon, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useInertRef } from "@/hooks/useInertRef";
import { cn } from "@/lib/utils";
import {
  type Thingy,
  type ThingyContent,
  createThingy,
  createThingyRevision,
  deleteThingy,
  publishThingyRevision,
  renameThingy,
} from "@/thingies/api/thingyApi";
import { ThingyFrame } from "@/thingies/host/ThingyFrame";
import { THINGY_SDK_VERSION } from "@/thingies/sdk/thingyProtocol";
import {
  type ThingyTemplate,
  THINGY_TEMPLATES,
  rememberThingyTemplate,
} from "@/thingies/templates/thingyTemplates";
import { ThingyErrorNotice } from "./ThingyErrorNotice";

/** The longest name the server takes. */
export const MAX_THINGY_NAME_LENGTH = 120;

/** What the blank template's name field starts with. */
const UNTITLED_THINGY = "Untitled thingy";

const TEMPLATE_ICONS: Record<ThingyTemplate["id"], LucideIcon> = {
  "where-was-i": HistoryIcon,
  "data-story": ChartLineIcon,
  blank: FilePlusIcon,
};

// Thumbnails render the template at four times the card's size, then scale it down.
const THUMBNAIL_HEIGHT = 120;
const THUMBNAIL_SCALE = 4;

/** A template running in the frame with in-memory sample data; nothing is saved. */
function TemplatePreview({ template, height }: { template: ThingyTemplate; height: number }) {
  const { thingy, content } = useMemo(() => {
    const previewThingy: Thingy = {
      id: `preview-${template.id}`,
      name: template.name,
      owner_id: "",
      viewer_id: "",
      published_revision: "preview",
      version: null,
      permissions: { read: true, write_data: true, publish: false, manage: false },
    };
    const previewContent: ThingyContent = {
      revision_id: "preview",
      html: template.revision?.html ?? "",
      sdk_version: THINGY_SDK_VERSION,
      capabilities: template.revision?.capabilities ?? [],
    };
    return { thingy: previewThingy, content: previewContent };
  }, [template]);
  if (!template.revision) return null;
  return (
    <div className="flex min-h-0" style={{ height }}>
      <ThingyFrame app={thingy} content={content} onRevisionChanged={() => undefined} preview />
    </div>
  );
}

/** The template itself, small and inert: a card shows what the thingy looks like. */
function TemplateThumbnail({ template }: { template: ThingyTemplate }) {
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
  templateId: ThingyTemplate["id"];
  thingy: Thingy;
  /** The template's page, once uploaded. */
  revisionId?: string;
  /** Whether the thingy is complete (published, for a template with a page). */
  done: boolean;
}

export interface ThingyTemplateGalleryProps {
  /** Runs once the thingy exists (and a template with a page has published it). */
  onCreated: (thingy: Thingy, template: ThingyTemplate) => void | Promise<void>;
  /** Narrowest card; the grid fits as many columns as the width allows. */
  minCardWidth?: number;
  /** Leaves out the blank template, where a description box beside the gallery starts one. */
  withoutBlank?: boolean;
}

export function ThingyTemplateGallery({
  onCreated,
  minCardWidth = 200,
  withoutBlank = false,
}: ThingyTemplateGalleryProps) {
  const [previewing, setPreviewing] = useState<ThingyTemplate | null>(null);
  const [naming, setNaming] = useState<ThingyTemplate | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(undefined);
  // How far a failed create got, so Create again continues from the failed step instead of
  // making another thingy, and Cancel deletes the half-made one.
  const [progress, setProgress] = useState<CreateProgress | null>(null);
  const nameFieldId = useId();

  const chooseTemplate = (template: ThingyTemplate) => {
    setPreviewing(null);
    setName(template.revision ? template.name : UNTITLED_THINGY);
    setCreateError(undefined);
    setProgress(null);
    setNaming(template);
  };

  const closeNaming = () => {
    // A thingy left half-made by a failed create goes again.
    if (progress !== null && !progress.done) void deleteThingy(progress.thingy.id).catch(() => {});
    setProgress(null);
    setNaming(null);
  };

  const trimmedName = name.trim();
  const nameTooLong = trimmedName.length > MAX_THINGY_NAME_LENGTH;
  const createFromTemplate = async (event?: FormEvent) => {
    event?.preventDefault();
    if (naming === null || !trimmedName || nameTooLong || creating) return;
    setCreating(true);
    setCreateError(undefined);
    let step: CreateProgress | null = progress?.templateId === naming.id ? progress : null;
    try {
      if (step === null) {
        step = { templateId: naming.id, thingy: await createThingy(trimmedName), done: false };
        setProgress(step);
      } else if (step.thingy.name !== trimmedName) {
        step = { ...step, thingy: await renameThingy(step.thingy.id, trimmedName) };
        setProgress(step);
      }
      const { thingy } = step;
      if (naming.revision) {
        const revisionId =
          step.revisionId ?? (await createThingyRevision(thingy.id, naming.revision)).id;
        step = { ...step, revisionId };
        setProgress(step);
        await publishThingyRevision(thingy.id, revisionId, thingy.version);
      }
      step = { ...step, done: true };
      setProgress(step);
      rememberThingyTemplate(thingy.id, naming.id);
      await onCreated(thingy, naming);
      setProgress(null);
      setNaming(null);
    } catch (error) {
      setCreateError(error);
    } finally {
      setCreating(false);
    }
  };

  const templates = THINGY_TEMPLATES.filter((template) => !withoutBlank || template.revision);
  return (
    <>
      <ul
        className="m-0 grid list-none gap-3 p-0"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${minCardWidth}px, 1fr))` }}
      >
        {templates.map((template) => (
          <li
            key={template.id}
            className={cn(
              "relative flex flex-col gap-2 rounded-[20px] border border-border bg-card p-3 transition-colors",
              "hover:border-foreground/20 hover:bg-muted/50",
              "has-[button:focus-visible]:ring-3 has-[button:focus-visible]:ring-ring/50",
            )}
          >
            <TemplateThumbnail template={template} />
            <button
              type="button"
              aria-label={template.name}
              aria-describedby={`thingy-template-${template.id}-description`}
              onClick={() =>
                template.revision ? setPreviewing(template) : chooseTemplate(template)
              }
              // The button covers the whole card.
              className="cursor-pointer text-left text-ui font-medium outline-none after:absolute after:inset-0 after:content-['']"
            >
              {template.name}
            </button>
            <span
              id={`thingy-template-${template.id}-description`}
              className="text-sm text-muted-foreground"
            >
              {template.description}
            </span>
          </li>
        ))}
      </ul>

      <Dialog open={previewing !== null} onOpenChange={(open) => !open && setPreviewing(null)}>
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
                <Button variant="outline" onClick={() => setPreviewing(null)}>
                  Close
                </Button>
                <Button
                  componentId="thingies.template.use"
                  onClick={() => chooseTemplate(previewing)}
                >
                  Use template
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={naming !== null}
        onOpenChange={(open) => {
          if (!open && !creating) closeNaming();
        }}
      >
        <DialogContent className="sm:max-w-md">
          <form
            onSubmit={(event) => void createFromTemplate(event)}
            className="flex flex-col gap-4"
          >
            <DialogHeader>
              <DialogTitle>Name your thingy</DialogTitle>
              <DialogDescription>You can rename it later.</DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={nameFieldId} className="text-sm font-medium text-muted-foreground">
                Name
              </label>
              <Input
                id={nameFieldId}
                value={name}
                autoFocus
                maxLength={MAX_THINGY_NAME_LENGTH}
                aria-invalid={nameTooLong || undefined}
                // The suggested name is replaced by typing, as a file name in a save dialog.
                onFocus={(event) => event.currentTarget.select()}
                onChange={(event) => setName(event.target.value)}
              />
              {nameTooLong && (
                <p className="text-sm text-destructive">
                  Use at most {MAX_THINGY_NAME_LENGTH} characters.
                </p>
              )}
            </div>
            {createError !== undefined && (
              <ThingyErrorNotice title="Couldn’t create the thingy" error={createError} />
            )}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button type="button" variant="outline" disabled={creating} onClick={closeNaming}>
                Cancel
              </Button>
              <Button
                type="submit"
                loading={creating}
                disabled={!trimmedName || nameTooLong}
                componentId="thingies.template.create"
              >
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** The gallery under an "Or start from a template" divider. */
export function ThingyTemplatesSection(props: ThingyTemplateGalleryProps) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-3">
      <div className="flex items-center gap-3 before:flex-1 before:border-t before:border-border before:content-[''] after:flex-1 after:border-t after:border-border after:content-['']">
        <h2 id={headingId} className="text-sm font-medium text-muted-foreground">
          Or start from a template
        </h2>
      </div>
      <ThingyTemplateGallery {...props} />
    </section>
  );
}
