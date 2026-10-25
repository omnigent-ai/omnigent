// In-memory stand-in for the Omnigent operations the workflow uses, for tests.

import { gunzipSync } from "node:zlib";
import type { WorkflowOmnigent } from "./workflow.ts";

export class FakeOmnigent implements WorkflowOmnigent {
  sessions: { id: string; metadata: Record<string, unknown>; spec: Record<string, unknown> }[] = [];
  messages: { sessionId: string; text: string; stableId?: string }[] = [];
  policies: { sessionId: string; id: string; name: string; expression: string }[] = [];
  interrupted: string[] = [];
  operations: string[] = [];

  async createBundledSession(bundle: Buffer, metadata: Record<string, unknown>) {
    const tar = gunzipSync(bundle);
    const size = parseInt(tar.subarray(124, 135).toString("ascii"), 8);
    const spec = JSON.parse(tar.subarray(512, 512 + size).toString("utf8")) as Record<string, unknown>;
    const id = `conv_${this.sessions.length + 1}`;
    this.sessions.push({ id, metadata, spec });
    this.operations.push(`create:${id}`);
    return { session_id: id };
  }
  async bindRunner(sessionId: string) {
    this.operations.push(`bind:${sessionId}`);
  }
  async sendMessage(sessionId: string, text: string, stableId?: string) {
    this.messages.push({ sessionId, text, stableId });
    return { queued: true };
  }
  async interrupt(sessionId: string) {
    this.interrupted.push(sessionId);
  }
  async addSessionPolicy(sessionId: string, name: string, expression: string) {
    const id = `pol_${this.policies.length + 1}`;
    this.policies.push({ sessionId, id, name, expression });
    this.operations.push(`policy+:${name}`);
    return { id };
  }
  async deleteSessionPolicy(sessionId: string, policyId: string) {
    this.policies = this.policies.filter((policy) => !(policy.sessionId === sessionId && policy.id === policyId));
    this.operations.push(`policy-:${policyId}`);
  }
  async listProjects() {
    return [{ id: "proj", name: "Shop" }];
  }
  sessionPolicies(sessionId: string) {
    return this.policies.filter((policy) => policy.sessionId === sessionId);
  }
}
