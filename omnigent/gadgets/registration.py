"""Which account registration owns gadgets, across password changes.

See :func:`gadget_registration`.
"""

from __future__ import annotations

from omnigent.stores.gadget_store import GadgetStore, NotFound

REGISTRATION_DIR = "registration"
REGISTRATION_FILE = f"{REGISTRATION_DIR}/link.json"
ALIASES = "aliases"


def gadget_registration(store: GadgetStore, user_id: str, generation: str | None) -> str | None:
    """The account registration that owns gadgets for *user_id* at *generation*.

    A password change or reset rotates an account's generation to revoke its sessions, but
    the account keeps its gadgets and shelf: they belong to the registration, named by the
    generation the account had when it first got one. ``DeletedAccountGadgets.carry``
    (:mod:`omnigent.gadgets.accounts`) links each later generation to it. A registration
    made again after a deletion starts with a new generation and no link, so it never
    reaches the old one's gadgets.
    """
    if generation is None:
        return None
    try:
        linked = store.user_files(user_id, owner_generation=generation).read_json(
            REGISTRATION_FILE
        )
    except NotFound:
        return generation
    registration = linked.get("registration")
    return registration if isinstance(registration, str) and registration else generation
