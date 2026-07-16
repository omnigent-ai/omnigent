"""Preference store — per-user client UI state that follows the account.

Each row is a ``(user_id, key, value)`` triple where ``value`` is an opaque
JSON string. Backs the sidebar pin set and the section collapse/expand sets so
they survive a fresh browser and follow the user across devices, with
``localStorage`` demoted to a fast local cache.
"""

from abc import ABC, abstractmethod

from omnigent.entities import UserPreference


class PreferenceStore(ABC):
    """Abstract base for per-user preference persistence.

    A generic key → JSON-value KV keyed by user. The route layer owns the
    allow-list of valid keys and the value-size cap; this store is a dumb
    persistence layer.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the preference store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get(self, user_id: str, key: str) -> UserPreference | None:
        """Look up a single preference.

        :param user_id: The owner, e.g. ``"alice@example.com"``.
        :param key: The preference name, e.g. ``"pinned_conversation_ids"``.
        :returns: The :class:`UserPreference` if set, otherwise ``None``.
        """
        ...

    @abstractmethod
    def get_all(self, user_id: str) -> dict[str, str]:
        """Return every preference for a user as a ``key`` → ``value`` map.

        :param user_id: The owner, e.g. ``"alice@example.com"``.
        :returns: A mapping of key to JSON-string value. Empty for an unknown
            user.
        """
        ...

    @abstractmethod
    def set(self, user_id: str, key: str, value: str) -> UserPreference:
        """Upsert a preference (overwrites any existing value for the key).

        :param user_id: The owner, e.g. ``"alice@example.com"``.
        :param key: The preference name, e.g. ``"pinned_conversation_ids"``.
        :param value: The JSON-string value, e.g. ``'["a","b"]'``.
        :returns: The resulting :class:`UserPreference`.
        """
        ...

    @abstractmethod
    def delete(self, user_id: str, key: str) -> bool:
        """Remove a preference.

        :param user_id: The owner, e.g. ``"alice@example.com"``.
        :param key: The preference name to remove.
        :returns: ``True`` if a row was deleted, ``False`` if none existed.
        """
        ...
