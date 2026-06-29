"""Push subscription store — browser Web Push registrations per user (#8).

The browser registers via ``upsert`` (keyed by endpoint, so re-subscribing the
same client refreshes its keys rather than duplicating); the sender reads via
``list_for_user``; a dead endpoint (HTTP 404/410 from the push service) is
pruned via ``delete_by_endpoint``.
"""

from abc import ABC, abstractmethod

from omnigent.entities.push_subscription import PushSubscription


class PushSubscriptionStore(ABC):
    """Abstract base for Web Push subscription persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        """
        :param user_id: The owning user.
        :returns: All of the user's subscriptions (possibly empty).
        """
        ...

    @abstractmethod
    def upsert(
        self,
        subscription_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> PushSubscription:
        """
        Register a subscription, keyed by ``endpoint``.

        If a row with this ``endpoint`` exists, its owner/keys are refreshed
        (a browser re-subscribing rotates its keys); otherwise a new row is
        created with ``subscription_id``.

        :param subscription_id: Id to use if creating (ignored on update).
        :param user_id: The owning user.
        :param endpoint: The push-service endpoint (unique key).
        :param p256dh: Client public key (base64url).
        :param auth: Client auth secret (base64url).
        :returns: The stored :class:`PushSubscription`.
        """
        ...

    @abstractmethod
    def delete_by_endpoint(self, endpoint: str) -> bool:
        """
        Remove a subscription by endpoint. Idempotent.

        :param endpoint: The push-service endpoint.
        :returns: ``True`` if a row was removed; ``False`` if none existed.
        """
        ...
