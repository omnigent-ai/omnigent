"""The marketplace's size and count limits, in one place.

The column bounds mirror the ``String(n)`` declarations on
``SqlInstalledSkill`` and ``SqlSkillSource``; a test pins the two together.
Every column stays far below the 16 KiB stored-value limit even at four
bytes a character.
"""

MIB = 1024 * 1024

# Indexing a checkout.
MAX_CATALOG_ENTRIES = 500
MAX_SKILL_MD_BYTES = MIB
MAX_MANIFEST_BYTES = MIB

# Fetching: bytes a clone may write to disk before it is killed.
MAX_CHECKOUT_BYTES = 500 * MIB

# Packaging: per skill, checked while walking, and per install request
# (compressed tarball bytes, since that is what is stored and delivered).
MAX_SKILL_BYTES = 20 * MIB
MAX_SKILL_ENTRIES = 2_000
MAX_INSTALL_BYTES = 100 * MIB
MAX_NAMES_PER_INSTALL = 20

# Installed rows per principal (a user, or the org), so delivery reads a known
# number of pages; and the compressed bytes a principal's enabled skills may
# total, checked at install, update and enable time. A session receives at
# most two principals' skills, so a served bundle never exceeds the sum.
MAX_INSTALLED_PER_OWNER = 200
MAX_DELIVERY_BYTES_PER_SCOPE = 100 * MIB
MAX_DELIVERY_BYTES = 2 * MAX_DELIVERY_BYTES_PER_SCOPE

# Paging.
MAX_PAGE = 200
DEFAULT_PAGE = 50

# Stored-column bounds, in characters.
MAX_NAME_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 1024
MAX_URL_CHARS = 255
MAX_REF_CHARS = 255
MAX_COMMIT_CHARS = 64
MAX_PATH_CHARS = 1024
MAX_LOCATION_CHARS = 512
MAX_USER_ID_CHARS = 128
# "user:<id>", "org" or "local".
MAX_PRINCIPAL_CHARS = 5 + MAX_USER_ID_CHARS
