"""Git-remote attribution shared by the built-in git-hosting policies.

A policy that gates git commands for one provider (``github`` today) asks
:func:`other_policy_owns_remote` whether another provider's own policy gates a
remote URL, and leaves that remote alone when it does. The provider descriptors
in :mod:`omnigent.git_providers` import only the standard library, which keeps
the lookup cheap inside the policy engine.
"""

from __future__ import annotations

from omnigent.git_providers import EnvInstances, load_facet, resolve_remote


def classify_remote(url: str) -> tuple[str, str] | None:
    """
    Name the git provider and repository that a remote URL points at.

    Hosts configured through ``OMNIGENT_GIT_PROVIDER_<ID>_HOSTS`` and ``GH_HOST``
    count, so a GitHub Enterprise remote resolves to ``github``.

    :param url: A git remote URL, e.g. ``"https://dev.azure.com/o/p/_git/r"`` or
        ``"git@github.com:o/r.git"``.
    :returns: ``(provider_id, repository)``, e.g. ``("azure_devops", "o/p/r")``, or
        ``None`` when no registered provider parses the URL (a remote name, a host
        no provider claims, or text that is not a URL).
    """
    parsed = resolve_remote(url, EnvInstances())
    return None if parsed is None else (parsed.provider, parsed.repository)


def other_policy_owns_remote(url: str, own_provider_id: str) -> bool:
    """
    Whether another git provider that has a policy of its own claims a remote URL.

    A provider that declares no policy facet has no policy to leave the remote to,
    so the asking policy keeps gating it.

    :param url: A git remote URL, e.g. ``"https://git.example.com/o/r.git"``.
    :param own_provider_id: The provider whose policy is asking, e.g. ``"github"``.
    :returns: ``True`` when a provider other than *own_provider_id* claims *url* and
        ``load_facet(provider_id, "policy")`` finds its policy.
    """
    owner = classify_remote(url)
    return (
        owner is not None
        and owner[0] != own_provider_id
        and load_facet(owner[0], "policy") is not None
    )
