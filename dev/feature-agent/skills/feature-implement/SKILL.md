---
name: feature-implement
description: Implement the design with focused tests mapped to each covered CUJ.
---

# Implement and test

1. **Implement the design.** Keep the change focused on the requirements. Follow
   the repository's conventions in `CLAUDE.md`, including framework-owned
   instructions, database query names, deprecation notes, and comment style.
2. **Test it.** Read `tests/README.md` and `tests/_helpers/README.md`, then the
   nearest existing scenario. Reuse or extend existing coverage before adding
   setup. Put input matrices at the lowest reliable layer; add E2E only for a
   distinct process, transport, or browser boundary.
   - Map every covered CUJ to at least one test that exercises the new behavior.
     There is no fail-before proof for new behavior, but each test must fail if
     the feature were removed; a test that only passes is not coverage.
   - Run the directly affected test modules and focused checks. Do not run the
     full repository suite locally; CI owns that.
   - Check environment-derived defaults with ambient variables populated, so a
     test does not pass only because of the local environment.
3. **Assess the impact.** Load `otto-impact-assessment` and apply it to the full
   diff. Record the results in the handoff's `impact_assessment` and
   `test_audit`.

If a requirement turns out to be infeasible, stop building it, mark its CUJs not
covered with the reason, and continue with the rest. Then load `feature-demo`.
