"""The marketplace's size and count limits, in one place.

The column bounds mirror the ``String(n)`` declarations on
``SqlInstalledSkill`` and ``SqlSkillSource``; a test pins the two together.
Every column stays far below the 16 KiB stored-value limit even at four
bytes a character.
"""

from dataclasses import dataclass

from omnigent.spec.tar_utils import DEFAULT_MAX_BYTES as RUNNER_UNPACK_BYTES
from omnigent.spec.tar_utils import DEFAULT_MAX_ENTRIES as RUNNER_UNPACK_ENTRIES

MIB = 1024 * 1024

# Indexing a checkout.
MAX_CATALOG_ENTRIES = 500
# Marketplace plugins followed from one manifest, and directories examined
# while looking for SKILL.md files, so a hostile manifest or tree stops early.
MAX_CATALOG_PLUGINS = 200
MAX_CATALOG_SCANNED_DIRS = 5_000
MAX_SKILL_MD_BYTES = MIB
MAX_MANIFEST_BYTES = MIB

# Fetching: what a clone may leave on disk (allocated bytes, and entries
# including directories) before it is killed or refused, and how long it may
# run. Clones are sparse, so these bound the checked-out part of a repository;
# an operator can change them under ``skills_marketplace.limits``.
MAX_CHECKOUT_BYTES = 500 * MIB
MAX_CHECKOUT_ENTRIES = 20_000
CLONE_TIMEOUT_S = 300


@dataclass(frozen=True)
class CloneLimits:
    """What one marketplace clone may use; ``skills_marketplace.limits`` overrides each."""

    checkout_bytes: int = MAX_CHECKOUT_BYTES
    checkout_entries: int = MAX_CHECKOUT_ENTRIES
    timeout_s: float = CLONE_TIMEOUT_S


DEFAULT_CLONE_LIMITS = CloneLimits()

# Packaging: per skill, checked while walking, and per install request
# (compressed tarball bytes, since that is what is stored and delivered).
MAX_SKILL_BYTES = 20 * MIB
MAX_SKILL_ENTRIES = 2_000
MAX_INSTALL_BYTES = 100 * MIB
MAX_NAMES_PER_INSTALL = 20

# Installed rows per principal (a user, or the org), so delivery reads a known
# number of pages; and the unpacked bytes and entries a principal's enabled
# skills may total, checked at install, update and enable time. A session
# receives two principals' skills inside its agent bundle, and the runner
# unpacks that bundle within RUNNER_UNPACK_*: each principal gets two fifths
# of those limits, leaving a fifth for the agent's own files.
MAX_INSTALLED_PER_OWNER = 200
MAX_DELIVERY_BYTES_PER_SCOPE = RUNNER_UNPACK_BYTES * 2 // 5
MAX_DELIVERY_ENTRIES_PER_SCOPE = RUNNER_UNPACK_ENTRIES * 2 // 5
MAX_DELIVERY_BYTES = 2 * MAX_DELIVERY_BYTES_PER_SCOPE
MAX_DELIVERY_ENTRIES = 2 * MAX_DELIVERY_ENTRIES_PER_SCOPE

# Paging.
MAX_PAGE = 200
DEFAULT_PAGE = 50

# The longest skill directory name the runner's spec validator accepts.
MAX_SKILL_NAME_CHARS = 64

# Stored-column bounds, in characters.
MAX_NAME_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 1024
MAX_URL_CHARS = 255
MAX_REF_CHARS = 255
# A source's directory inside its repository; the identity key must fit MySQL's.
MAX_SOURCE_PATH_CHARS = 200
MAX_COMMIT_CHARS = 64
MAX_PATH_CHARS = 1024
MAX_LOCATION_CHARS = 512
MAX_USER_ID_CHARS = 128
# "user:<id>", "org" or "local".
MAX_PRINCIPAL_CHARS = 5 + MAX_USER_ID_CHARS
