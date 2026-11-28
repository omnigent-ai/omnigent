"""DOM geometry probes shared by browser and full-stack UI tests."""

from playwright.sync_api import Locator


def workspace_bar_needs_collapse(bar: Locator) -> bool:
    """Whether the composer workspace bar's full labels would overflow or truncate.

    Mirrors the bar's own rule (any ``[data-workspace-collapse-label]`` wider
    than its box, or the row wider than the bar) by probing the expanded layout
    in place and restoring the current verdict within the same evaluation, so a
    test can assert the icon collapse is justified — and absent when everything fits.

    :param bar: Locator for ``composer-workspace-controls``.
    :returns: ``True`` when the bar must show icons only.
    """
    return bar.evaluate(
        """bar => {
          const verdict = bar.dataset.labels;
          delete bar.dataset.labels;
          const labels = [...bar.querySelectorAll('[data-workspace-collapse-label]')];
          const cramped = bar.scrollWidth > bar.clientWidth + 1
            || labels.some(el => el.scrollWidth > el.clientWidth + 1);
          if (verdict !== undefined) bar.dataset.labels = verdict;
          return cramped;
        }"""
    )
