"""Gadgets: small web apps agents build for people, kept beside their sessions.

* :mod:`~omnigent.gadgets.service` — one viewer's operations, used by the
  ``/v1/gadgets`` routes (:mod:`omnigent.server.gadgets`).
* :mod:`~omnigent.gadgets.access` — :class:`GadgetAccess`, who may do what.
* :mod:`~omnigent.gadgets.documents` — revisioned records on create-only files.
* :mod:`~omnigent.gadgets.constraints` — guides and cross-record rules.
* :mod:`~omnigent.gadgets.actions` — declared actions and the action ledger.
* :mod:`~omnigent.gadgets.progress` — action progress from session state.

Storage is a :class:`~omnigent.stores.gadget_store.GadgetStore`. See
``designs/GADGETS.md``.
"""
