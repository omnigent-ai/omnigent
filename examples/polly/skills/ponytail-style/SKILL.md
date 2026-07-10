---
name: ponytail-style
description: Keep implementation dispatches narrowly scoped, minimal, and free of speculative code.
when-to-use: Use for implementation tasks where a sub-agent may over-engineer or add code outside the request, especially refactors and bug fixes in established code.
---

# ponytail-style — minimal implementation discipline

When dispatching an implementation task, extend the sub-agent's task packet
with the following constraints. Keep the original acceptance contract intact;
these constraints govern how the agent satisfies it.

- Apply YAGNI: do not write code for hypothetical future needs.
- Produce the smallest diff that satisfies the acceptance criteria.
- Do not expand the scope. If the task says to fix X, do not also address Y.
- Leave no dead code, TODOs, placeholders, or unused compatibility paths.
- Avoid premature abstractions. Extract shared code only when a third concrete
  use warrants it.
- Add tests only for behavior introduced or changed by this task.
- Prefer self-explanatory code. Add comments sparingly, and explain why rather
  than what.

Tell the implementer to call out any requested outcome that cannot be achieved
within these constraints instead of silently broadening the task. Apply the
same constraints when routing review fixes back to the implementer.
