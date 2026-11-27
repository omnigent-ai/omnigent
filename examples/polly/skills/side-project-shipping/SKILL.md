---
name: side-project-shipping
description: Reconcile a personal-project shipping queue with live PR, release, and owner evidence, then advance authorized work through verification.
---

# Side-project shipping

Use the portfolio board or queue supplied by the user together with current PRs and owner updates. Keep a disposition for each assigned project. Verify the current owner, default branch, PR head and base, and release status before assigning work. Check whether the same change already shipped or is active elsewhere.

Pick the next concrete, unclaimed gate. If Compound Engineering LFG or Ship is available and requested, use it for the scoped task. Apply Polly's worktree, implementation, and cross-review workflow. Record the published PR, tested commit, checks that actually ran, and remaining dependencies. For stacked PRs, recheck the base and CI after a parent lands.

Keep source, local tests, hosted checks, packaging, publication, and live acceptance distinct. A billing failure before runner startup leaves the check unverified. A test on one platform verifies that platform. For an external wait, record the event, where to verify it, and the action it unlocks. Advance independent work while that event is pending.
