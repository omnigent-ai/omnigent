// Where the floating panel may start an agent: this Mac, once the desktop shell has said which
// host it is, or a new sandbox. Pure, so the dialog and its tests share one answer.

/** A host as the policy needs it. */
export interface PanelHostFacts {
  host_id: string;
  status: string;
}

export interface PanelDestinationFacts {
  /** This Mac's host id: undefined while the shell hasn't answered, null when it has none. */
  thisMacHostId: string | null | undefined;
  /** The host list, or null while it loads. */
  hosts: readonly PanelHostFacts[] | null;
  /** Whether new sandboxes can start; null while the server's answer loads. */
  sandboxesOffered: boolean | null;
}

export interface PanelDestinations {
  /** Still learning which host is this Mac, or what the server offers. */
  pending: boolean;
  /** This Mac's host, when the shell named it and the server lists it. */
  thisMac: { hostId: string; online: boolean } | null;
  sandboxes: boolean;
}

export function panelDestinations(facts: PanelDestinationFacts): PanelDestinations {
  const pending =
    facts.thisMacHostId === undefined || facts.hosts === null || facts.sandboxesOffered === null;
  const listed =
    typeof facts.thisMacHostId === "string"
      ? facts.hosts?.find((host) => host.host_id === facts.thisMacHostId)
      : undefined;
  return {
    pending,
    thisMac: listed ? { hostId: listed.host_id, online: listed.status === "online" } : null,
    sandboxes: facts.sandboxesOffered === true,
  };
}

/** Whether the panel may target `hostId`: only this Mac's own host, and nothing while pending. */
export function panelAllowsHost(destinations: PanelDestinations, hostId: string | null): boolean {
  return !destinations.pending && hostId !== null && destinations.thisMac?.hostId === hostId;
}
