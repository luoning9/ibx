from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import (
    resolve_metric_allowed_rules,
    resolve_metric_allowed_windows,
    resolve_trigger_window_policy,
)


UTC = timezone.utc


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ConditionEvaluationState:
    condition_id: str
    state: str
    last_value: float | None = None


@dataclass(frozen=True)
class StrategyEvaluationResult:
    outcome: str
    condition_met: bool
    decision_reason: str
    metrics: dict[str, Any]
    condition_states: list[ConditionEvaluationState]


@dataclass(frozen=True)
class ContractDataRequirement:
    contract_id: int | None
    base_bar: str
    required_points: int
    state_requirements: list[dict[str, Any]]
    include_partial_bar: bool


@dataclass(frozen=True)
class ConditionDataRequirement:
    condition_id: str
    require_time_alignment: bool
    missing_data_policy: str
    contracts: list[ContractDataRequirement]


@dataclass(frozen=True)
class PreparedCondition:
    condition_id: str
    condition_raw: dict[str, Any]
    metric: str
    trigger_mode: str
    evaluation_window: str
    operator: str
    threshold: float | None
    requirement: ConditionDataRequirement


@dataclass(frozen=True)
class ConditionEvaluationResult:
    state: str
    observed_value: float | None
    reason: str


@dataclass(frozen=True)
class ConditionEvaluationInput:
    values_by_contract: dict[int, list[float]]
    state_values: dict[str, Any]


def _gateway_is_working() -> bool:
    raw = os.getenv("IBX_GATEWAY_READY")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _evaluate_condition(operator: str, threshold: float | None, observed_value: float) -> bool:
    if threshold is None:
        return False
    if operator == ">=":
        return observed_value >= threshold
    if operator == "<=":
        return observed_value <= threshold
    return False


def _resolve_condition_id(condition: dict[str, Any]) -> str:
    raw_cid = condition.get("condition_id")
    if isinstance(raw_cid, str) and raw_cid.strip():
        return raw_cid.strip()
    raise ValueError("condition_id is required")


def _parse_window_to_seconds(window: str) -> int:
    text = str(window or "").strip().lower()
    if not text:
        return 0
    unit = text[-1]
    amount_raw = text[:-1]
    try:
        amount = int(amount_raw)
    except ValueError:
        return 0
    if amount <= 0:
        return 0
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    if unit == "d":
        return amount * 86400
    return 0


def _estimated_required_points(
    *,
    trigger_mode: str,
    evaluation_window: str,
    base_bar: str,
    confirm_consecutive: int,
    confirm_ratio: float,
) -> int:
    mode = trigger_mode.strip().upper()
    if mode == "LEVEL_INSTANT":
        return 1
    if mode in {"CROSS_UP_INSTANT", "CROSS_DOWN_INSTANT"}:
        return 2
    window_seconds = _parse_window_to_seconds(evaluation_window)
    base_seconds = _parse_window_to_seconds(base_bar)
    if window_seconds <= 0 or base_seconds <= 0:
        base_points = 1
    else:
        base_points = max(1, math.ceil(window_seconds / base_seconds))
    confirm_points = max(confirm_consecutive, int(math.ceil(confirm_ratio * base_points)))
    if mode in {"CROSS_UP_CONFIRM", "CROSS_DOWN_CONFIRM"}:
        return confirm_points + 1
    return confirm_points


def _require_time_alignment(metric: str) -> bool:
    return metric.strip().upper() in {"SPREAD", "VOLUME_RATIO", "AMOUNT_RATIO"}

def _state_requirements(metric: str, contract_id: int | None) -> list[dict[str, Any]]:
    metric_key = metric.strip().upper()
    if contract_id is None:
        return []
    if metric_key == "DRAWDOWN_PCT":
        return [
            {
                "type": "since_activation_extrema",
                "contract_id": contract_id,
                "need_high": True,
                "need_low": False,
            }
        ]
    if metric_key == "RALLY_PCT":
        return [
            {
                "type": "since_activation_extrema",
                "contract_id": contract_id,
                "need_high": False,
                "need_low": True,
            }
        ]
    return []


def _build_trigger_policy_payload(prepared: PreparedCondition) -> dict[str, Any]:
    req = prepared.requirement
    first_contract = req.contracts[0] if req.contracts else None
    return {
        "condition_id": req.condition_id,
        "trigger_mode": prepared.trigger_mode,
        "evaluation_window": prepared.evaluation_window,
        "missing_data_policy": req.missing_data_policy,
        "require_time_alignment": req.require_time_alignment,
        "contracts": [
            {
                "contract_id": contract.contract_id,
                "base_bar": contract.base_bar,
                "required_points": contract.required_points,
                "include_partial_bar": contract.include_partial_bar,
                "state_requirements": contract.state_requirements,
            }
            for contract in req.contracts
        ],
        "base_bar": first_contract.base_bar if first_contract else None,
        "required_points": first_contract.required_points if first_contract else None,
        "include_partial_bar": first_contract.include_partial_bar if first_contract else None,
    }


def _condition_id_hint(condition: dict[str, Any], fallback: str) -> str:
    raw = condition.get("condition_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback


def _values_for_contract(
    values_by_contract: dict[int, list[float]],
    contract_id: int,
) -> list[float] | None:
    values = values_by_contract.get(contract_id)
    if values is None:
        values = values_by_contract.get(str(contract_id))  # type: ignore[arg-type]
    if values is None:
        return None
    if not isinstance(values, list):
        return None
    out: list[float] = []
    for item in values:
        value = _to_float_or_none(item)
        if value is None:
            return None
        out.append(value)
    return out


def _metric_observed_value(
    *,
    metric: str,
    contract_values: dict[int, float],
    state_values: dict[str, Any],
    first_contract_id: int,
    second_contract_id: int | None,
) -> float | None:
    metric_key = metric.strip().upper()
    primary = contract_values.get(first_contract_id)
    if primary is None:
        return None
    if metric_key == "PRICE":
        return primary
    if metric_key == "DRAWDOWN_PCT":
        high = _to_float_or_none(state_values.get("since_activation_high"))
        if high is None or high <= 0:
            return None
        return (high - primary) / high
    if metric_key == "RALLY_PCT":
        low = _to_float_or_none(state_values.get("since_activation_low"))
        if low is None or low <= 0:
            return None
        return (primary - low) / low
    if second_contract_id is None:
        return None
    secondary = contract_values.get(second_contract_id)
    if secondary is None:
        return None
    if metric_key == "SPREAD":
        return primary - secondary
    if metric_key in {"VOLUME_RATIO", "AMOUNT_RATIO"}:
        if secondary <= 0:
            return None
        return primary / secondary
    return None


def _prepare_condition(condition: dict[str, Any]) -> PreparedCondition:
    condition_id = _resolve_condition_id(condition)
    trigger_mode = str(condition.get("trigger_mode", "LEVEL_INSTANT"))
    evaluation_window = str(condition.get("evaluation_window", "1m"))
    policy = resolve_trigger_window_policy(trigger_mode, evaluation_window)
    metric = str(condition.get("metric", ""))
    metric_key = metric.strip().upper()
    if not metric_key:
        raise ValueError("metric is required")
    contract_id = _to_int_or_none(condition.get("contract_id"))
    contract_id_b = _to_int_or_none(condition.get("contract_id_b"))
    if contract_id is None:
        raise ValueError("contract_id is required")
    if _require_time_alignment(metric) and contract_id_b is None:
        raise ValueError("contract_id_b is required")
    operator = str(condition.get("operator", "")).strip()
    allowed_rules = resolve_metric_allowed_rules(metric_key)
    if (policy.trigger_mode, operator) not in allowed_rules:
        raise ValueError(
            f"metric={metric_key} does not allow trigger_mode={policy.trigger_mode} with operator={operator}"
        )
    allowed_windows = resolve_metric_allowed_windows(metric_key)
    if policy.evaluation_window not in allowed_windows:
        raise ValueError(f"metric={metric_key} does not allow evaluation_window={policy.evaluation_window}")
    raw_threshold = condition.get("value")
    threshold = _to_float_or_none(raw_threshold)
    if raw_threshold is None:
        raise ValueError("value is required")
    if threshold is None:
        raise ValueError("value must be a number")
    required_points = _estimated_required_points(
        trigger_mode=policy.trigger_mode,
        evaluation_window=policy.evaluation_window,
        base_bar=policy.base_bar,
        confirm_consecutive=policy.confirm_consecutive,
        confirm_ratio=policy.confirm_ratio,
    )
    contracts: list[ContractDataRequirement] = [
        ContractDataRequirement(
            contract_id=contract_id,
            base_bar=policy.base_bar,
            required_points=required_points,
            state_requirements=_state_requirements(metric, contract_id),
            include_partial_bar=policy.include_partial_bar,
        )
    ]
    if _require_time_alignment(metric):
        contracts.append(
            ContractDataRequirement(
                contract_id=contract_id_b,
                base_bar=policy.base_bar,
                required_points=required_points,
                state_requirements=[],
                include_partial_bar=policy.include_partial_bar,
            )
        )
    requirement = ConditionDataRequirement(
        condition_id=condition_id,
        require_time_alignment=_require_time_alignment(metric),
        missing_data_policy=policy.missing_data_policy,
        contracts=contracts,
    )
    return PreparedCondition(
        condition_id=condition_id,
        condition_raw=condition,
        metric=metric_key,
        trigger_mode=policy.trigger_mode,
        evaluation_window=policy.evaluation_window,
        operator=operator,
        threshold=threshold,
        requirement=requirement,
    )


class ConditionEvaluator:
    def __init__(self, condition: dict[str, Any]) -> None:
        self.condition = condition
        self.prepared: PreparedCondition | None = None

    def prepare(self) -> ConditionDataRequirement:
        self.prepared = _prepare_condition(self.condition)
        return self.prepared.requirement

    def evaluate(self, evaluation_input: ConditionEvaluationInput) -> ConditionEvaluationResult:
        if self.prepared is None:
            raise ValueError("prepare must be called before evaluate")
        requirement = self.prepared.requirement
        if not requirement.contracts:
            return ConditionEvaluationResult(
                state="WAITING",
                observed_value=None,
                reason="missing_contract_requirements",
            )

        by_contract: dict[int, list[float]] = {}
        for contract_req in requirement.contracts:
            if contract_req.contract_id is None:
                return ConditionEvaluationResult(
                    state="WAITING",
                    observed_value=None,
                    reason="missing_contract_id",
                )
            series = _values_for_contract(evaluation_input.values_by_contract, contract_req.contract_id)
            if series is None:
                return ConditionEvaluationResult(
                    state="WAITING",
                    observed_value=None,
                    reason=f"missing_contract_values:{contract_req.contract_id}",
                )
            if len(series) < contract_req.required_points:
                return ConditionEvaluationResult(
                    state="WAITING",
                    observed_value=None,
                    reason=f"insufficient_points:{contract_req.contract_id}",
                )
            by_contract[contract_req.contract_id] = series

        first_contract_id = requirement.contracts[0].contract_id
        second_contract_id = requirement.contracts[1].contract_id if len(requirement.contracts) > 1 else None
        assert first_contract_id is not None
        current_contract_values = {
            cid: values[-1]
            for cid, values in by_contract.items()
        }
        observed_value = _metric_observed_value(
            metric=self.prepared.metric,
            contract_values=current_contract_values,
            state_values=evaluation_input.state_values,
            first_contract_id=first_contract_id,
            second_contract_id=second_contract_id,
        )
        if observed_value is None:
            return ConditionEvaluationResult(
                state="WAITING",
                observed_value=None,
                reason="missing_metric_inputs",
            )

        mode = self.prepared.trigger_mode
        if mode.startswith("CROSS_"):
            previous_contract_values: dict[int, float] = {}
            for cid, values in by_contract.items():
                if len(values) < 2:
                    return ConditionEvaluationResult(
                        state="WAITING",
                        observed_value=None,
                        reason=f"insufficient_points_for_cross:{cid}",
                    )
                previous_contract_values[cid] = values[-2]
            previous_observed_value = _metric_observed_value(
                metric=self.prepared.metric,
                contract_values=previous_contract_values,
                state_values=evaluation_input.state_values,
                first_contract_id=first_contract_id,
                second_contract_id=second_contract_id,
            )
            if previous_observed_value is None or self.prepared.threshold is None:
                return ConditionEvaluationResult(
                    state="WAITING",
                    observed_value=None,
                    reason="missing_cross_inputs",
                )
            if mode.startswith("CROSS_UP"):
                passed = previous_observed_value < self.prepared.threshold and observed_value >= self.prepared.threshold
            else:
                passed = previous_observed_value > self.prepared.threshold and observed_value <= self.prepared.threshold
        else:
            passed = _evaluate_condition(self.prepared.operator, self.prepared.threshold, observed_value)

        return ConditionEvaluationResult(
            state="TRUE" if passed else "FALSE",
            observed_value=observed_value,
            reason="evaluated",
        )


def evaluate_strategy(
    strategy_row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> StrategyEvaluationResult:
    evaluated_at = _to_utc(now or datetime.now(UTC))
    conditions_raw = json.loads(strategy_row["conditions_json"] or "[]")
    if not isinstance(conditions_raw, list):
        conditions_raw = []

    if not conditions_raw:
        return StrategyEvaluationResult(
            outcome="no_conditions_configured",
            condition_met=False,
            decision_reason="no_conditions_configured",
            metrics={
                "evaluation_engine": "skeleton_v1",
                "evaluated_at": _to_iso_utc(evaluated_at),
                "conditions": 0,
                "trigger_policies": [],
            },
            condition_states=[],
        )

    evaluators: list[ConditionEvaluator] = []
    trigger_policies: list[dict[str, Any]] = []
    for idx, condition in enumerate(conditions_raw, start=1):
        condition_dict: dict[str, Any] = condition if isinstance(condition, dict) else {}
        evaluator = ConditionEvaluator(condition_dict)
        try:
            evaluator.prepare()
        except ValueError as exc:
            invalid_condition_id = _condition_id_hint(condition_dict, fallback=f"c{idx}")
            return StrategyEvaluationResult(
                outcome="condition_config_invalid",
                condition_met=False,
                decision_reason="condition_config_invalid",
                metrics={
                    "evaluation_engine": "skeleton_v1",
                    "evaluated_at": _to_iso_utc(evaluated_at),
                    "conditions": len(conditions_raw),
                    "trigger_policies": trigger_policies,
                    "invalid_condition_id": invalid_condition_id,
                    "error": str(exc),
                },
                condition_states=[
                    ConditionEvaluationState(
                        condition_id=invalid_condition_id,
                        state="NOT_EVALUATED",
                    )
                ],
            )
        evaluators.append(evaluator)
        trigger_policies.append(_build_trigger_policy_payload(evaluator.prepared))

    if not _gateway_is_working():
        condition_states = [
            ConditionEvaluationState(condition_id=evaluator.prepared.condition_id, state="NOT_EVALUATED")
            for evaluator in evaluators
        ]
        return StrategyEvaluationResult(
            outcome="gateway_not_work",
            condition_met=False,
            decision_reason="gateway_not_work",
            metrics={
                "evaluation_engine": "skeleton_v1",
                "evaluated_at": _to_iso_utc(evaluated_at),
                "conditions": len(condition_states),
                "trigger_policies": trigger_policies,
            },
            condition_states=condition_states,
        )

    condition_states: list[ConditionEvaluationState] = []
    condition_results: list[bool] = []
    has_waiting = False
    for evaluator in evaluators:
        condition_id = evaluator.prepared.condition_id
        condition_dict = evaluator.prepared.condition_raw
        values_by_contract: dict[int, list[float]] = {}
        condition_values = condition_dict.get("values_by_contract")
        if isinstance(condition_values, dict):
            for key, values in condition_values.items():
                cid = _to_int_or_none(key)
                if cid is None:
                    continue
                if isinstance(values, list):
                    values_by_contract[cid] = list(values)
                else:
                    scalar = _to_float_or_none(values)
                    if scalar is not None:
                        values_by_contract[cid] = [scalar]
        # Skeleton bridge: fallback single observed value into first contract series.
        if evaluator.prepared.requirement.contracts:
            first_contract_id = evaluator.prepared.requirement.contracts[0].contract_id
            fallback_value = _to_float_or_none(
                condition_dict.get("observed_value", condition_dict.get("last_value"))
            )
            if first_contract_id is not None and fallback_value is not None and first_contract_id not in values_by_contract:
                values_by_contract[first_contract_id] = [fallback_value]
        state_values = condition_dict.get("state_values")
        if not isinstance(state_values, dict):
            state_values = {}
        condition_result = evaluator.evaluate(
            ConditionEvaluationInput(
                values_by_contract=values_by_contract,
                state_values=state_values,
            )
        )
        if condition_result.state == "WAITING":
            has_waiting = True
            condition_states.append(
                ConditionEvaluationState(
                    condition_id=condition_id,
                    state=condition_result.state,
                )
            )
            continue
        condition_results.append(condition_result.state == "TRUE")
        condition_states.append(
            ConditionEvaluationState(
                condition_id=condition_id,
                state=condition_result.state,
                last_value=condition_result.observed_value,
            )
        )

    condition_logic = str(strategy_row["condition_logic"] or "AND").upper()
    if condition_logic == "AND" and any(not item for item in condition_results):
        return StrategyEvaluationResult(
            outcome="evaluated",
            condition_met=False,
            decision_reason="conditions_not_met",
            metrics={
                "evaluation_engine": "skeleton_v1",
                "evaluated_at": _to_iso_utc(evaluated_at),
                "condition_logic": condition_logic,
                "conditions": len(condition_states),
                "trigger_policies": trigger_policies,
            },
            condition_states=condition_states,
        )
    if condition_logic != "AND" and any(condition_results):
        return StrategyEvaluationResult(
            outcome="evaluated",
            condition_met=True,
            decision_reason="conditions_met",
            metrics={
                "evaluation_engine": "skeleton_v1",
                "evaluated_at": _to_iso_utc(evaluated_at),
                "condition_logic": condition_logic,
                "conditions": len(condition_states),
                "trigger_policies": trigger_policies,
            },
            condition_states=condition_states,
        )
    if has_waiting:
        return StrategyEvaluationResult(
            outcome="waiting_for_market_data",
            condition_met=False,
            decision_reason="waiting_for_market_data",
            metrics={
                "evaluation_engine": "skeleton_v1",
                "evaluated_at": _to_iso_utc(evaluated_at),
                "condition_logic": condition_logic,
                "conditions": len(condition_states),
                "trigger_policies": trigger_policies,
            },
            condition_states=condition_states,
        )

    condition_met = all(condition_results) if condition_logic == "AND" else any(condition_results)
    return StrategyEvaluationResult(
        outcome="evaluated",
        condition_met=condition_met,
        decision_reason="conditions_met" if condition_met else "conditions_not_met",
        metrics={
            "evaluation_engine": "skeleton_v1",
            "evaluated_at": _to_iso_utc(evaluated_at),
            "condition_logic": condition_logic,
            "conditions": len(condition_states),
            "trigger_policies": trigger_policies,
        },
        condition_states=condition_states,
    )


def persist_evaluation_result(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    evaluated_at: datetime,
    result: StrategyEvaluationResult,
) -> None:
    evaluated_at_utc = _to_utc(evaluated_at)
    ts_iso = _to_iso_utc(evaluated_at_utc)

    conn.execute(
        """
        INSERT INTO strategy_runs (strategy_id, evaluated_at, condition_met, decision_reason, metrics_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            strategy_id,
            ts_iso,
            1 if result.condition_met else 0,
            result.decision_reason,
            _dumps_json(result.metrics),
        ),
    )

    for state in result.condition_states:
        cursor = conn.execute(
            """
            UPDATE condition_states
            SET state = ?, last_value = ?, last_evaluated_at = ?, updated_at = ?
            WHERE strategy_id = ? AND condition_id = ?
            """,
            (
                state.state,
                state.last_value,
                ts_iso,
                ts_iso,
                strategy_id,
                state.condition_id,
            ),
        )
        if cursor.rowcount > 0:
            continue
        conn.execute(
            """
            INSERT INTO condition_states (
                strategy_id, condition_id, state, last_value, last_evaluated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                state.condition_id,
                state.state,
                state.last_value,
                ts_iso,
                ts_iso,
            ),
        )
