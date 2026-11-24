import { streamOpenAICompletions } from "@oh-my-pi/pi-ai/providers/openai-completions";
import installGatewayCompatibility from "./omnigent_gateway.js";

// Use the concrete provider, not streamSimple: the latter dispatches back to
// the custom API registered below and would recurse.
export default function installOmpGateway(pi) {
  installGatewayCompatibility(pi, (model, context, options = {}) =>
    streamOpenAICompletions(model, context, {
      ...options,
      maxTokens: options.maxTokens ?? model.maxTokens,
      maxTokensExplicit: options.maxTokens !== undefined,
      disableReasoning: options.disableReasoning || options.forceReasoningOff,
    }),
  );
}
