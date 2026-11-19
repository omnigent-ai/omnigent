// Starting a new thingy: describe it, or make one from a template. The New thingy dialog and the
// create page show it. Build makes the thingy and opens New session with the thingy beside the
// composer and the build instructions in it; the viewer picks where the session runs and sends it.
import { useCallback } from "react";

import { PageScroll } from "@/components/PageScroll";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Link, useNavigate } from "@/lib/routing";
import { type Thingy, createThingy } from "@/thingies/api/thingyApi";
import { composeThingyBuildPrompt, thingyNameFrom } from "@/thingies/host/thingyBuild";
import { thingyBoardNewSessionUrl, thingyBoardPath } from "@/thingies/host/thingyPages";
import { markThingySessionPending } from "@/thingies/host/thingySession";
import type { ThingyTemplate } from "@/thingies/templates/thingyTemplates";
import { ThingyDescribeForm } from "./ThingyDescribeForm";
import { ThingyTemplatesSection } from "./ThingyTemplateGallery";
import { appPathFromUrl } from "./thingyNavigation";

const START_EXPLANATION =
  "Say what it shows, what data it uses and what people do with it. Build opens a new session with the thingy beside it; send the message there to build it.";

function ThingyStartContent({
  onCancel,
  onLeave,
}: {
  onCancel?: () => void;
  /** After it opens the new thingy somewhere else. */
  onLeave?: () => void;
}) {
  const navigate = useNavigate();
  const openNewSession = useCallback(
    (thingy: Thingy, prompt: string) => {
      markThingySessionPending(thingy.id);
      navigate(appPathFromUrl(thingyBoardNewSessionUrl(thingy.id, prompt)));
      onLeave?.();
    },
    [navigate, onLeave],
  );
  const build = async (description: string) => {
    const thingy = await createThingy(thingyNameFrom(description));
    openNewSession(thingy, `${composeThingyBuildPrompt(thingy)}${description}`);
  };
  const onTemplateCreated = (thingy: Thingy, template: ThingyTemplate) => {
    if (template.revision) {
      navigate(thingyBoardPath(thingy.id));
      onLeave?.();
    } else {
      // A blank thingy is built in a new session, which then shows it beside itself.
      openNewSession(thingy, composeThingyBuildPrompt(thingy));
    }
  };
  return (
    <>
      <ThingyDescribeForm
        explanation={START_EXPLANATION}
        onBuild={build}
        onCancel={onCancel}
        errorTitle="Couldn’t create the thingy"
      />
      <ThingyTemplatesSection onCreated={onTemplateCreated} />
    </>
  );
}

/** New thingy, opened by the Thingy page's New as Automations' New opens New automation. */
export function ThingyStartDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const close = useCallback(() => onOpenChange(false), [onOpenChange]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[90vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-[720px]"
        data-testid="new-thingy-dialog"
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>New thingy</DialogTitle>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-6 py-5">
          {open && <ThingyStartContent onCancel={close} onLeave={close} />}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** `/thingy/new`: the dialog's content as a page. */
export function ThingyStartPage() {
  const navigate = useNavigate();
  return (
    <PageScroll className="[scrollbar-gutter:stable]" contentClassName="px-4 md:px-6">
      <nav aria-label="Breadcrumb" className="mb-1 text-ui text-muted-foreground">
        <Link to="/thingy" className="hover:text-foreground">
          Thingy
        </Link>
      </nav>
      <h1 className="mb-6 text-2xl font-medium">New thingy</h1>
      <div className="flex flex-col gap-6">
        <ThingyStartContent onCancel={() => navigate("/thingy")} />
      </div>
    </PageScroll>
  );
}
