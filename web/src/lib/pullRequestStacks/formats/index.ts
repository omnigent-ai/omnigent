// The stack description formats this build reads, in the order they're tried: the ones its
// Pull Requests page integrations register.

import { pullRequestIntegrations } from "@/lib/pullRequestIntegrations";
import type { StackFormat } from "@/lib/pullRequestStacks/stackModel";

export function stackFormats(): readonly StackFormat[] {
  return pullRequestIntegrations().stackFormats;
}
