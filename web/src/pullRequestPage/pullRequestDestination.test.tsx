// How long a request to open a control lives: a pull request view takes it once its control is
// ready, a section that remounts hands it on, and the view leaving, moving to another PR or
// reading as another account drops it, even before anything was ready to take it.

import { act, cleanup, render, screen } from "@testing-library/react";
import { type ReactNode, StrictMode, useMemo, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MacReadAsContext } from "@/hooks/macReadAs";
import type * as Identity from "@/lib/identity";
import type { MacNamedReader } from "@/lib/macNamedReads";

import {
  requestPullRequestControl,
  resetControlRequestsForTests,
  waitingControlFor,
} from "./controlRequests";
import {
  PullRequestDestinationContext,
  PullRequestRowDestinationContext,
  useControlDestination,
  useRowControlPlace,
  useTakenControlRequest,
} from "./pullRequestDestination";

const workspace = vi.hoisted(() => ({ owner: "user-1" }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof Identity>()),
  getCurrentUserId: () => workspace.owner,
}));

const PR = "https://github.com/acme/app/pull/42";
const OTHER = "https://github.com/acme/app/pull/43";
const BOB: MacNamedReader = { host: "github.com", login: "bob" };
const VIEW = "page-detail";

function LabelsSection({ prUrl, ready }: { prUrl: string; ready: boolean }) {
  const [open, setOpen] = useState(false);
  useTakenControlRequest(prUrl, "labels", ready, () => setOpen(true), open);
  return (
    <button type="button" aria-expanded={open} onClick={() => setOpen(false)}>
      Labels
    </button>
  );
}

/** A pull request view, as the panel is one, with its Labels section once `sections` show. */
function View({ prUrl = PR, ready = true, sections = true, sectionKey = "labels" }) {
  const { value } = useControlDestination(VIEW, prUrl, true);
  return (
    <PullRequestDestinationContext.Provider value={value}>
      {sections && <LabelsSection key={`${prUrl}:${sectionKey}`} prUrl={prUrl} ready={ready} />}
    </PullRequestDestinationContext.Provider>
  );
}

function ReadingAs({ reader, children }: { reader: MacNamedReader | null; children: ReactNode }) {
  const readAs = useMemo(() => ({ reader }), [reader]);
  return <MacReadAsContext.Provider value={readAs}>{children}</MacReadAsContext.Provider>;
}

const ask = (reader: MacNamedReader | null = null) =>
  act(() => requestPullRequestControl(PR, "labels", { destination: VIEW, reader }, null));
const labels = () => screen.queryByRole("button", { name: "Labels" });

beforeEach(() => {
  workspace.owner = "user-1";
  resetControlRequestsForTests();
});
afterEach(() => cleanup());

describe("a request to open a control", () => {
  it("opens in its view once the control is ready", () => {
    const view = render(<View ready={false} />);
    ask();
    expect(labels()).toHaveAttribute("aria-expanded", "false");
    view.rerender(<View />);
    expect(labels()).toHaveAttribute("aria-expanded", "true");
  });

  it("is dropped when the reader changes before the control is ready, even back again", () => {
    const tree = (reader: MacNamedReader | null, ready: boolean) => (
      <ReadingAs reader={reader}>
        <View ready={ready} />
      </ReadingAs>
    );
    const view = render(tree(BOB, false));
    ask(BOB);
    view.rerender(tree(null, false));
    view.rerender(tree(BOB, true));
    expect(labels()).toHaveAttribute("aria-expanded", "false");
  });

  it("is dropped when the workspace user changes while the view shows no sections yet", () => {
    const view = render(<View sections={false} />);
    ask();
    workspace.owner = "user-2";
    view.rerender(<View sections={false} />);
    workspace.owner = "user-1";
    view.rerender(<View />);
    expect(labels()).toHaveAttribute("aria-expanded", "false");
  });

  it("is dropped by the list that made it once that list reads as another account", () => {
    function Row({ reader }: { reader: MacNamedReader | null }) {
      return (
        <ReadingAs reader={reader}>
          <PullRequestRowDestinationContext.Provider value={VIEW}>
            <RowPlace />
          </PullRequestRowDestinationContext.Provider>
        </ReadingAs>
      );
    }
    function RowPlace() {
      useRowControlPlace();
      return null;
    }
    // Nothing shows the PR yet: the request waits for a view still to open.
    const list = render(<Row reader={BOB} />);
    ask(BOB);
    list.rerender(<Row reader={null} />);
    list.rerender(<Row reader={BOB} />);
    expect(waitingControlFor(VIEW, PR)).toBeNull();
  });

  it("isn't dropped by StrictMode running the view's effects twice", () => {
    render(
      <StrictMode>
        <View ready={false} />
      </StrictMode>,
    );
    ask();
    expect(waitingControlFor(VIEW, PR)).toBe("labels");
  });

  it("isn't taken again after the view moves to another PR and back", () => {
    const view = render(<View />);
    ask();
    expect(labels()).toHaveAttribute("aria-expanded", "true");
    view.rerender(<View prUrl={OTHER} />);
    view.rerender(<View />);
    expect(labels()).toHaveAttribute("aria-expanded", "false");
  });

  it("passes to the section that replaces one that went with its picker up", () => {
    const view = render(<View />);
    ask();
    view.rerender(<View sectionKey="replacement" />);
    expect(labels()).toHaveAttribute("aria-expanded", "true");
  });

  it("is gone once the whole view leaves, though its section had taken it", () => {
    const view = render(<View />);
    ask();
    expect(labels()).toHaveAttribute("aria-expanded", "true");
    view.rerender(<div />);
    expect(waitingControlFor(VIEW, PR)).toBeNull();
    view.rerender(<View />);
    expect(labels()).toHaveAttribute("aria-expanded", "false");
  });
});
