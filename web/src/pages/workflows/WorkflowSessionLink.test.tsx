import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { RoutingProvider, basenamedRouting, reactRouterRouting } from "@/lib/routing";
import { WorkflowSessionLink } from "./WorkflowSessionLink";

afterEach(cleanup);

it.each(["enabled", "disabled", "worker", "invalid", "embedded"])(
  "links the workflow root: %s",
  (scenario) => {
    const workflowId = "a".repeat(32);
    render(
      <MemoryRouter>
        <RoutingProvider
          value={scenario === "embedded" ? basenamedRouting("/omni") : reactRouterRouting}
        >
          <CapabilitiesProvider
            info={{
              ...FALLBACK_SERVER_INFO,
              features: { dynamic_workflows: scenario !== "disabled" },
            }}
          >
            <WorkflowSessionLink
              labels={{
                "omnigent.workflow.id": scenario === "invalid" ? "bad/id" : workflowId,
                "omnigent.workflow.root": scenario === "worker" ? "false" : "true",
              }}
            />
          </CapabilitiesProvider>
        </RoutingProvider>
      </MemoryRouter>,
    );
    const link = screen.queryByRole("link", { name: "Open workflow designer" });
    if (["disabled", "worker", "invalid"].includes(scenario)) expect(link).not.toBeInTheDocument();
    else
      expect(link).toHaveAttribute(
        "href",
        (scenario === "embedded" ? "/omni" : "") + "/workflows?workflow=" + workflowId,
      );
  },
);
