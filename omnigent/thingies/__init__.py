"""Thingies: small web apps agents build for people, kept beside their sessions.

* :mod:`~omnigent.thingies.service` — one viewer's operations, used by the
  ``/v1/thingies`` routes (:mod:`omnigent.server.thingies`).
* :mod:`~omnigent.thingies.access` — :class:`ThingyAccess`, who may do what.
* :mod:`~omnigent.thingies.documents` — revisioned records on create-only files.
* :mod:`~omnigent.thingies.constraints` — guides and cross-record rules.
* :mod:`~omnigent.thingies.actions` — declared actions and the action ledger.
* :mod:`~omnigent.thingies.progress` — action progress from session state.

Storage is a :class:`~omnigent.stores.thingy_store.ThingyStore`. See
``designs/THINGIES.md``.
"""
