from collections import OrderedDict


class SkillLoadGuard:
    def __init__(self, max_scopes: int = 256) -> None:
        self._max_scopes = max_scopes
        self._loaded: OrderedDict[tuple[str, str], set[str]] = OrderedDict()

    @staticmethod
    def _scope(
        conversation_id: str | None,
        task_id: str | None,
    ) -> tuple[str, str] | None:
        if not isinstance(task_id, str) or not task_id:
            return None
        return conversation_id or "", task_id

    def is_loaded(
        self,
        conversation_id: str | None,
        task_id: str | None,
        skill_name: str,
    ) -> bool:
        scope = self._scope(conversation_id, task_id)
        return scope is not None and skill_name in self._loaded.get(scope, set())

    def record(
        self,
        conversation_id: str | None,
        task_id: str | None,
        skill_name: str,
    ) -> None:
        scope = self._scope(conversation_id, task_id)
        if scope is None:
            return
        self._loaded.setdefault(scope, set()).add(skill_name)
        self._loaded.move_to_end(scope)
        while len(self._loaded) > self._max_scopes:
            self._loaded.popitem(last=False)


skill_load_guard = SkillLoadGuard()
