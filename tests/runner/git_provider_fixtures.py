"""Install descriptors in the cached registry for isolated dispatcher tests."""

from omnigent import git_providers
from omnigent.git_providers import GitProvider


def register_provider(descriptor: GitProvider) -> None:
    git_providers._providers = (*git_providers.providers(), descriptor)
