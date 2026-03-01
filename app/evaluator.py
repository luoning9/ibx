from __future__ import annotations

import json
import logging
import math
import os
import socket
import sqlite3
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .config import (
    infer_ib_api_port,
    load_app_config,
    resolve_metric_allowed_rules,
    resolve_metric_allowed_windows,
    resolve_trigger_window_policy,
)


UTC = timezone.utc
_GATEWAY_PROBE_CACHE_LOCK = Lock()
_GATEWAY_PROBE_CACHE: tuple[float, bool] | None = None
_LOGGER = logging.getLogger("ibx.evaluator")


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
    last_evaluated_at: datetime | None = None


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
    state_values: dict[str, Any] | None = None


def _normalize_values_by_contract(
    raw_values: dict[Any, Any] | None,
) -> dict[int, list[float]]:
    if not isinstance(raw_values, dict):
        return {}
    values_by_contract: dict[int, list[float]] = {}
    for key, values in raw_values.items():
        cid = _to_int_or_none(key)
        if cid is None:
            continue
        if isinstance(values, list):
            numeric_values: list[float] = []
            valid = True
            for item in values:
                numeric = _to_float_or_none(item)
                if numeric is None:
                    valid = False
                    break
                numeric_values.append(numeric)
            if valid:
                values_by_contract[cid] = numeric_values
            continue
        scalar = _to_float_or_none(values)
        if scalar is not None:
            values_by_contract[cid] = [scalar]
    return values_by_contract


def _gateway_override_from_env() -> bool | None:
    raw = os.getenv("IBX_GATEWAY_READY")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _gateway_probe_ttl_seconds() -> float:
    raw = os.getenv("IBX_GATEWAY_PROBE_TTL_SECONDS")
    default = 2.0
    try:
        ttl = float(raw) if raw is not None else default
    except ValueError:
        ttl = default
    return max(0.0, ttl)


def _gateway_probe_timeout_seconds() -> float:
    cfg_timeout = float(load_app_config().ib_gateway.timeout_seconds)
    default = min(2.0, max(0.2, cfg_timeout))
    raw = os.getenv("IBX_GATEWAY_PROBE_TIMEOUT_SECONDS")
    try:
        timeout = float(raw) if raw is not None else default
    except ValueError:
        timeout = default
    return min(5.0, max(0.1, timeout))


def _resolve_gateway_probe_target() -> tuple[str, int]:
    cfg = load_app_config().ib_gateway
    host = str(os.getenv("IB_HOST", cfg.host)).strip() or cfg.host
    mode = str(os.getenv("TRADING_MODE", cfg.trading_mode)).strip().lower()
    raw_port = str(os.getenv("IB_API_PORT", "")).strip()
    if raw_port:
        try:
            parsed = int(raw_port)
            if 1 <= parsed <= 65535:
                return host, parsed
        except ValueError:
            pass
    return host, int(infer_ib_api_port(mode))


def _read_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("socket closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(sock: socket.socket) -> bytes:
    header = _read_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length <= 0:
        return b""
    return _read_exact(sock, length)


def _probe_gateway_health_once() -> bool:
    host, port = _resolve_gateway_probe_target()
    timeout = _gateway_probe_timeout_seconds()
    min_client_version = 157
    max_client_version = 178
    payload = f"v{min_client_version}..{max_client_version}".encode("ascii")
    framed = struct.pack(">I", len(payload)) + payload
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"API\0" + framed)
            reply = _read_frame(sock)
            if not reply:
                return False
            parts = reply.split(b"\0")
            version = parts[0].decode("ascii", errors="ignore").strip() if parts else ""
            return bool(version)
    except Exception:
        return False


def _gateway_is_working() -> bool:
    global _GATEWAY_PROBE_CACHE
    override = _gateway_override_from_env()
    if override is not None:
        return override

    now = time.monotonic()
    ttl = _gateway_probe_ttl_seconds()
    with _GATEWAY_PROBE_CACHE_LOCK:
        cached = _GATEWAY_PROBE_CACHE
        if cached is not None and (now - cached[0]) <= ttl:
            return cached[1]

    status = _probe_gateway_health_once()
    with _GATEWAY_PROBE_CACHE_LOCK:
        _GATEWAY_PROBE_CACHE = (now, status)
    return status


def reset_gateway_probe_cache() -> None:
    global _GATEWAY_PROBE_CACHE
    with _GATEWAY_PROBE_CACHE_LOCK:
        _GATEWAY_PROBE_CACHE = None


def gateway_is_working() -> bool:
    return _gateway_is_working()


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


def _extract_requirement_keys(metrics: dict[str, Any]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    policies = metrics.get("trigger_policies")
    if not isinstance(policies, list):
        return keys
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        condition_id = str(policy.get("condition_id") or "").strip()
        if not condition_id:
            continue
        contracts = policy.get("contracts")
        if not isinstance(contracts, list):
            continue
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            contract_id = _to_int_or_none(contract.get("contract_id"))
            if contract_id is None:
                continue
            keys.add((condition_id, contract_id))
    return keys


def _parse_monitoring_end_map(raw: Any) -> dict[str, dict[str, str]]:
    # strategy_runs.last_monitoring_data_end_at shape: {condition_id: {contract_id: ts_iso}}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for condition_id, by_contract in data.items():
        if not isinstance(condition_id, str) or not isinstance(by_contract, dict):
            continue
        normalized_contracts: dict[str, str] = {}
        for contract_id, value in by_contract.items():
            if not isinstance(contract_id, str):
                continue
            value_text = str(value or "").strip()
            if value_text:
                normalized_contracts[contract_id] = value_text
        if normalized_contracts:
            out[condition_id] = normalized_contracts
    return out


def _set_monitoring_end_value(
    values: dict[str, dict[str, str]],
    *,
    condition_id: str,
    contract_id: int,
    ts_iso: str,
) -> None:
    by_contract = values.setdefault(condition_id, {})
    by_contract[str(contract_id)] = ts_iso


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
        observed_series: list[float] = []
        if requirement.require_time_alignment and len(by_contract) > 1:
            aligned_points = min(len(values) for values in by_contract.values())
            for idx in range(aligned_points):
                contract_values = {
                    cid: values[-aligned_points + idx]
                    for cid, values in by_contract.items()
                }
                observed = _metric_observed_value(
                    metric=self.prepared.metric,
                    contract_values=contract_values,
                    state_values=evaluation_input.state_values,
                    first_contract_id=first_contract_id,
                    second_contract_id=second_contract_id,
                )
                if observed is not None:
                    observed_series.append(observed)
        else:
            primary_values = by_contract[first_contract_id]
            aligned_points = len(primary_values)
            secondary_values: list[float] | None = None
            if second_contract_id is not None and second_contract_id in by_contract:
                secondary_values = by_contract[second_contract_id]
                aligned_points = min(aligned_points, len(secondary_values))
            for idx in range(aligned_points):
                contract_values: dict[int, float] = {
                    first_contract_id: primary_values[-aligned_points + idx]
                }
                if secondary_values is not None and second_contract_id is not None:
                    contract_values[second_contract_id] = secondary_values[-aligned_points + idx]
                observed = _metric_observed_value(
                    metric=self.prepared.metric,
                    contract_values=contract_values,
                    state_values=evaluation_input.state_values,
                    first_contract_id=first_contract_id,
                    second_contract_id=second_contract_id,
                )
                if observed is not None:
                    observed_series.append(observed)

        if not observed_series:
            return ConditionEvaluationResult(
                state="WAITING",
                observed_value=None,
                reason="missing_metric_inputs",
            )
        observed_value = observed_series[-1]

        mode = self.prepared.trigger_mode
        if mode.startswith("CROSS_"):
            if len(observed_series) < 2 or self.prepared.threshold is None:
                return ConditionEvaluationResult(
                    state="WAITING",
                    observed_value=None,
                    reason="missing_cross_inputs",
                )
            if mode.startswith("CROSS_UP"):
                passed = any(
                    prev < self.prepared.threshold <= curr
                    for prev, curr in zip(observed_series, observed_series[1:])
                )
            else:
                passed = any(
                    prev > self.prepared.threshold >= curr
                    for prev, curr in zip(observed_series, observed_series[1:])
                )
        else:
            passed = any(
                _evaluate_condition(self.prepared.operator, self.prepared.threshold, sample)
                for sample in observed_series
            )

        return ConditionEvaluationResult(
            state="TRUE" if passed else "FALSE",
            observed_value=observed_value,
            reason="evaluated",
        )


def evaluate_strategy(
    strategy_row: sqlite3.Row,
    *,
    now: datetime | None = None,
    condition_inputs: dict[str, ConditionEvaluationInput] | None = None,
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
            ConditionEvaluationState(
                condition_id=evaluator.prepared.condition_id,
                state="NOT_EVALUATED",
                last_evaluated_at=evaluated_at,
            )
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
        external_input = None if condition_inputs is None else condition_inputs.get(condition_id)
        values_by_contract = _normalize_values_by_contract(
            None if external_input is None else external_input.values_by_contract
        )
        condition_values = _normalize_values_by_contract(condition_dict.get("values_by_contract"))
        for cid, values in condition_values.items():
            values_by_contract.setdefault(cid, values)
        # Skeleton bridge: fallback single observed value into first contract series.
        if evaluator.prepared.requirement.contracts:
            first_contract_id = evaluator.prepared.requirement.contracts[0].contract_id
            fallback_value = _to_float_or_none(
                condition_dict.get("observed_value", condition_dict.get("last_value"))
            )
            if first_contract_id is not None and fallback_value is not None and first_contract_id not in values_by_contract:
                values_by_contract[first_contract_id] = [fallback_value]
        state_values = (
            None
            if external_input is None
            else external_input.state_values
        )
        if not isinstance(state_values, dict):
            state_values = condition_dict.get("state_values")
        if not isinstance(state_values, dict):
            state_values = {}
        points_by_contract: dict[int, int] = {}
        for cid, series in values_by_contract.items():
            points_by_contract[cid] = len(series)
        required_points_by_contract = {
            int(contract_req.contract_id): int(contract_req.required_points)
            for contract_req in evaluator.prepared.requirement.contracts
            if contract_req.contract_id is not None
        }
        condition_result = evaluator.evaluate(
            ConditionEvaluationInput(
                values_by_contract=values_by_contract,
                state_values=state_values,
            )
        )
        _LOGGER.info(
            "condition evaluate condition_id=%s metric=%s trigger_mode=%s evaluation_window=%s "
            "state=%s reason=%s observed_value=%s points_by_contract=%s required_points_by_contract=%s",
            condition_id,
            evaluator.prepared.metric,
            evaluator.prepared.trigger_mode,
            evaluator.prepared.evaluation_window,
            condition_result.state,
            condition_result.reason,
            condition_result.observed_value,
            points_by_contract,
            required_points_by_contract,
        )
        if condition_result.state == "WAITING":
            has_waiting = True
            condition_states.append(
                ConditionEvaluationState(
                    condition_id=condition_id,
                    state=condition_result.state,
                    last_evaluated_at=evaluated_at,
                )
            )
            continue
        condition_results.append(condition_result.state == "TRUE")
        condition_states.append(
            ConditionEvaluationState(
                condition_id=condition_id,
                state=condition_result.state,
                last_value=condition_result.observed_value,
                last_evaluated_at=evaluated_at,
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
    updated_at: datetime,
    evaluated_at: datetime | None,
    initial_last_monitoring_data_end_at: datetime | None,
    monitoring_end_updates: dict[tuple[str, int], datetime],
    suggested_next_monitor_at: datetime | None,
    result: StrategyEvaluationResult,
) -> None:
    updated_at_utc = _to_utc(updated_at)
    updated_at_iso = _to_iso_utc(updated_at_utc)
    evaluated_at_utc = _to_utc(evaluated_at) if evaluated_at is not None else None
    evaluated_at_iso = _to_iso_utc(evaluated_at_utc) if evaluated_at_utc is not None else None
    initial_monitoring_end_iso = _to_iso_utc(
        _to_utc(initial_last_monitoring_data_end_at or evaluated_at_utc or updated_at_utc)
    )
    requirement_keys = _extract_requirement_keys(result.metrics)
    existing = conn.execute(
        """
        SELECT last_monitoring_data_end_at, evaluated_at
        FROM strategy_runs
        WHERE strategy_id = ?
        """,
        (strategy_id,),
    ).fetchone()

    monitoring_end_map: dict[str, dict[str, str]] = {}
    if existing is not None:
        monitoring_end_map = _parse_monitoring_end_map(existing["last_monitoring_data_end_at"])
    for condition_id, contract_id in requirement_keys:
        by_contract = monitoring_end_map.get(condition_id)
        if by_contract is not None and str(contract_id) in by_contract:
            continue
        prev_iso: str | None = None
        if by_contract is not None:
            prev_iso = by_contract.get(str(contract_id))
        _set_monitoring_end_value(
            monitoring_end_map,
            condition_id=condition_id,
            contract_id=contract_id,
            ts_iso=initial_monitoring_end_iso,
        )
        _LOGGER.info(
            "strategy_runs last_monitoring_data_end_at update strategy_id=%s condition_id=%s contract_id=%s "
            "prev=%s next=%s reason=%s",
            strategy_id,
            condition_id,
            contract_id,
            prev_iso,
            initial_monitoring_end_iso,
            "initialize",
        )
    for (condition_id, contract_id), monitoring_end_at in monitoring_end_updates.items():
        prev_iso: str | None = None
        by_contract = monitoring_end_map.get(condition_id)
        if by_contract is not None:
            prev_iso = by_contract.get(str(contract_id))
        next_iso = _to_iso_utc(monitoring_end_at)
        if prev_iso == next_iso:
            continue
        _set_monitoring_end_value(
            monitoring_end_map,
            condition_id=condition_id,
            contract_id=contract_id,
            ts_iso=next_iso,
        )
        _LOGGER.info(
            "strategy_runs last_monitoring_data_end_at update strategy_id=%s condition_id=%s contract_id=%s "
            "prev=%s next=%s reason=%s",
            strategy_id,
            condition_id,
            contract_id,
            prev_iso,
            next_iso,
            "market_data",
        )

    monitoring_end_json = _dumps_json(monitoring_end_map)
    suggested_next_monitor_iso = (
        _to_iso_utc(suggested_next_monitor_at)
        if suggested_next_monitor_at is not None
        else None
    )
    stored_evaluated_at_iso = evaluated_at_iso
    if stored_evaluated_at_iso is None:
        if existing is not None and isinstance(existing["evaluated_at"], str) and existing["evaluated_at"].strip():
            stored_evaluated_at_iso = str(existing["evaluated_at"]).strip()
        else:
            stored_evaluated_at_iso = updated_at_iso
    if existing is not None:
        conn.execute(
            """
            UPDATE strategy_runs
            SET evaluated_at = ?,
                last_monitoring_data_end_at = ?,
                suggested_next_monitor_at = ?,
                condition_met = ?,
                decision_reason = ?,
                last_outcome = ?,
                check_count = check_count + 1,
                metrics_json = ?,
                updated_at = ?
            WHERE strategy_id = ?
            """,
            (
                stored_evaluated_at_iso,
                monitoring_end_json,
                suggested_next_monitor_iso,
                1 if result.condition_met else 0,
                result.decision_reason,
                result.outcome,
                _dumps_json(result.metrics),
                updated_at_iso,
                strategy_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id,
                last_monitoring_data_end_at,
                suggested_next_monitor_at,
                first_evaluated_at,
                evaluated_at,
                condition_met,
                decision_reason,
                last_outcome,
                check_count,
                metrics_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                strategy_id,
                monitoring_end_json,
                suggested_next_monitor_iso,
                stored_evaluated_at_iso,
                stored_evaluated_at_iso,
                1 if result.condition_met else 0,
                result.decision_reason,
                result.outcome,
                _dumps_json(result.metrics),
                updated_at_iso,
            ),
        )

    for state in result.condition_states:
        state_last_evaluated_at_iso = (
            _to_iso_utc(_to_utc(state.last_evaluated_at))
            if state.last_evaluated_at is not None
            else None
        )
        cursor = conn.execute(
            """
            UPDATE condition_states
            SET state = ?, last_value = ?, last_evaluated_at = COALESCE(?, last_evaluated_at), updated_at = ?
            WHERE strategy_id = ? AND condition_id = ?
            """,
            (
                state.state,
                state.last_value,
                state_last_evaluated_at_iso,
                updated_at_iso,
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
                state_last_evaluated_at_iso,
                updated_at_iso,
            ),
        )
