from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.config import ModuleConfig, ScoringConfig
from issue_prioritization.domain import Issue, ScoreResult, ScoreStep

_CENT = Decimal("0.01")


class ScoreModule(Protocol):
    name: str

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep: ...


@dataclass(frozen=True)
class ComponentModule:
    catalog: AreaCatalog
    config: ModuleConfig
    name: str = "component"

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep:
        weight = self.catalog.weight_for(issue, self.config.decimal("default_weight"))
        return _multiply_step(self.name, score, weight)


@dataclass(frozen=True)
class DuplicateModule:
    config: ModuleConfig
    name: str = "duplicates"

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep:
        bonus = min(
            self.config.decimal("max_bonus"),
            self.config.decimal("increment") * issue.duplicate_count,
        )
        return _multiply_step(self.name, score, Decimal(1) + bonus)


@dataclass(frozen=True)
class DemandModule:
    config: ModuleConfig
    name: str = "demand"

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep:
        cap = int(self.config.decimal("upvote_cap"))
        upvotes = min(issue.upvote_count, cap)
        points = (
            self.config.decimal("max_points") * Decimal(upvotes) / Decimal(cap)
            if cap
            else Decimal(0)
        )
        return _add_step(self.name, score, points)


@dataclass(frozen=True)
class ReadinessModule:
    config: ModuleConfig
    name: str = "readiness"

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep:
        if issue.needs_info:
            multiplier = self.config.decimal("needs_info_multiplier")
        elif issue.is_ready:
            multiplier = self.config.decimal("ready_multiplier")
        else:
            multiplier = Decimal(1)
        return _multiply_step(self.name, score, multiplier)


@dataclass(frozen=True)
class AgeModule:
    config: ModuleConfig
    name: str = "age"

    def apply(self, issue: Issue, score: Decimal) -> ScoreStep:
        if issue.age_days <= self.config.decimal("fresh_days"):
            multiplier = self.config.decimal("fresh_multiplier")
        elif issue.age_days <= self.config.decimal("visibility_days"):
            multiplier = self.config.decimal("visibility_multiplier")
        else:
            multiplier = self.config.decimal("stale_multiplier")
        return _multiply_step(self.name, score, multiplier)


class ScoreEngine:
    def __init__(self, config: ScoringConfig, catalog: AreaCatalog) -> None:
        self.config = config
        modules: list[ScoreModule] = []
        for name in config.module_order:
            module_config = config.modules[name]
            if not module_config.enabled:
                continue
            if name == "component":
                modules.append(ComponentModule(catalog, module_config))
            elif name == "duplicates":
                modules.append(DuplicateModule(module_config))
            elif name == "demand":
                modules.append(DemandModule(module_config))
            elif name == "readiness":
                modules.append(ReadinessModule(module_config))
            elif name == "age":
                modules.append(AgeModule(module_config))
            else:
                raise ValueError(f"unsupported scoring module: {name}")
        self.modules = tuple(modules)

    def score(self, issue: Issue) -> ScoreResult:
        score = self.config.severity_weights[issue.severity]
        steps = [ScoreStep("severity", "set", score, Decimal(0), score)]
        if issue.needs_info:
            score = Decimal(0)
            steps.append(ScoreStep("needs_info", "set", score, steps[-1].score_after, score))
        else:
            for module in self.modules:
                step = module.apply(issue, score)
                steps.append(step)
                score = step.score_after
        score = _round(score)
        return ScoreResult(
            score=score,
            priority=self.config.priority_for(score),
            steps=tuple(steps),
        )


def _multiply_step(name: str, score: Decimal, multiplier: Decimal) -> ScoreStep:
    return ScoreStep(name, "multiply", multiplier, score, _round(score * multiplier))


def _add_step(name: str, score: Decimal, points: Decimal) -> ScoreStep:
    return ScoreStep(name, "add", _round(points), score, _round(score + points))


def _round(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)
