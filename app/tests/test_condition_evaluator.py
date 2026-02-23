from __future__ import annotations

import pytest

from app.config import (
    SUPPORTED_TRIGGER_MODES,
    resolve_metric_allowed_rules,
    resolve_metric_allowed_windows,
    resolve_trigger_window_policy,
)
from app.evaluator import ConditionEvaluationInput, ConditionEvaluator


ALL_METRICS = ("PRICE", "DRAWDOWN_PCT", "RALLY_PCT", "VOLUME_RATIO", "AMOUNT_RATIO", "SPREAD")
PAIR_METRICS = {"VOLUME_RATIO", "AMOUNT_RATIO", "SPREAD"}
ALL_WINDOWS = ("1m", "5m", "30m", "1h", "2h", "4h", "1d", "2d")
ALL_OPERATORS = (">=", "<=")


def _condition_type_for_metric(metric: str) -> str:
    return "PAIR_PRODUCTS" if metric in PAIR_METRICS else "SINGLE_PRODUCT"


def _build_condition_payload(
    *,
    metric: str,
    trigger_mode: str,
    operator: str,
    evaluation_window: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "condition_id": "cm1",
        "condition_type": _condition_type_for_metric(metric),
        "metric": metric,
        "trigger_mode": trigger_mode,
        "evaluation_window": evaluation_window,
        "operator": operator,
        "value": 1.0,
        "product": "AAPL",
        "contract_id": 101,
    }
    if metric in PAIR_METRICS:
        payload["product_b"] = "MSFT"
        payload["contract_id_b"] = 202
    return payload


def test_condition_evaluator_prepare_resolves_policy() -> None:
    evaluator = ConditionEvaluator(
        {
            "condition_id": "c1",
            "condition_type": "SINGLE_PRODUCT",
            "metric": "PRICE",
            "trigger_mode": "LEVEL_INSTANT",
            "evaluation_window": "1m",
            "operator": ">=",
            "value": 10,
            "product": "AAPL",
            "contract_id": 265598,
        }
    )
    requirement = evaluator.prepare()
    prepared = evaluator.prepared

    assert requirement == prepared.requirement
    assert prepared.condition_id == "c1"
    assert prepared.threshold == 10.0
    assert prepared.requirement.require_time_alignment is False
    assert prepared.requirement.missing_data_policy == "fail"
    assert len(prepared.requirement.contracts) == 1
    contract_req = prepared.requirement.contracts[0]
    assert contract_req.contract_id == 265598
    assert contract_req.required_points == 1
    assert contract_req.state_requirements == []
    assert contract_req.base_bar == "1m"
    assert contract_req.include_partial_bar is True


def test_condition_evaluator_waiting_when_market_data_missing() -> None:
    evaluator = ConditionEvaluator(
        {
            "condition_id": "c1",
            "condition_type": "SINGLE_PRODUCT",
            "metric": "PRICE",
            "trigger_mode": "LEVEL_INSTANT",
            "evaluation_window": "1m",
            "operator": ">=",
            "value": 10,
            "product": "AAPL",
            "contract_id": 265598,
        }
    )
    evaluator.prepare()

    result = evaluator.evaluate(
        ConditionEvaluationInput(
            values_by_contract={},
            state_values={},
        )
    )
    assert result.state == "WAITING"
    assert result.observed_value is None


def test_condition_evaluator_true_false() -> None:
    evaluator = ConditionEvaluator(
        {
            "condition_id": "c1",
            "condition_type": "SINGLE_PRODUCT",
            "metric": "PRICE",
            "trigger_mode": "LEVEL_INSTANT",
            "evaluation_window": "1m",
            "operator": ">=",
            "value": 10,
            "product": "AAPL",
            "contract_id": 265598,
        }
    )
    evaluator.prepare()

    prepared = evaluator.prepared
    assert prepared is not None
    contract_id = prepared.requirement.contracts[0].contract_id
    assert contract_id is not None
    passed = evaluator.evaluate(
        ConditionEvaluationInput(
            values_by_contract={contract_id: [10.5]},
            state_values={},
        )
    )
    failed = evaluator.evaluate(
        ConditionEvaluationInput(
            values_by_contract={contract_id: [9.9]},
            state_values={},
        )
    )

    assert passed.state == "TRUE"
    assert failed.state == "FALSE"


def test_condition_evaluator_metric_specific_requirements() -> None:
    spread_evaluator = ConditionEvaluator(
        {
            "condition_id": "c-spread",
            "condition_type": "PAIR_PRODUCTS",
            "metric": "SPREAD",
            "trigger_mode": "CROSS_UP_CONFIRM",
            "evaluation_window": "30m",
            "operator": ">=",
            "value": 5,
            "product": "AAPL",
            "product_b": "MSFT",
            "contract_id": 11,
            "contract_id_b": 22,
        }
    )
    spread_evaluator.prepare()
    spread_prepared = spread_evaluator.prepared
    assert spread_prepared.requirement.require_time_alignment is True
    assert len(spread_prepared.requirement.contracts) == 2
    first_contract = spread_prepared.requirement.contracts[0]
    second_contract = spread_prepared.requirement.contracts[1]
    assert first_contract.contract_id == 11
    assert second_contract.contract_id == 22
    assert first_contract.required_points == 4
    assert second_contract.required_points == 4
    assert first_contract.state_requirements == []
    assert second_contract.state_requirements == []

    drawdown_evaluator = ConditionEvaluator(
        {
            "condition_id": "c-dd",
            "condition_type": "SINGLE_PRODUCT",
            "metric": "DRAWDOWN_PCT",
            "trigger_mode": "LEVEL_CONFIRM",
            "evaluation_window": "5m",
            "operator": ">=",
            "value": 0.1,
            "product": "AAPL",
            "contract_id": 11,
        }
    )
    drawdown_evaluator.prepare()
    drawdown_prepared = drawdown_evaluator.prepared
    assert drawdown_prepared.requirement.require_time_alignment is False
    assert len(drawdown_prepared.requirement.contracts) == 1
    drawdown_contract = drawdown_prepared.requirement.contracts[0]
    assert drawdown_contract.state_requirements == [
        {
            "type": "since_activation_extrema",
            "contract_id": 11,
            "need_high": True,
            "need_low": False,
        }
    ]


def test_condition_evaluator_prepare_requires_contract_id() -> None:
    with pytest.raises(ValueError, match="contract_id is required"):
        ConditionEvaluator(
            {
                "condition_id": "c1",
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "operator": ">=",
                "value": 10,
                "product": "AAPL",
            }
        ).prepare()


def test_condition_evaluator_prepare_requires_contract_id_b_for_pair_metric() -> None:
    with pytest.raises(ValueError, match="contract_id_b is required"):
        ConditionEvaluator(
            {
                "condition_id": "c1",
                "condition_type": "PAIR_PRODUCTS",
                "metric": "SPREAD",
                "trigger_mode": "LEVEL_CONFIRM",
                "evaluation_window": "5m",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
                "product_b": "MSFT",
                "contract_id": 1,
            }
        ).prepare()


def test_condition_evaluator_prepare_requires_numeric_threshold() -> None:
    with pytest.raises(ValueError, match="value is required"):
        ConditionEvaluator(
            {
                "condition_id": "c1",
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "operator": ">=",
                "product": "AAPL",
                "contract_id": 1,
            }
        ).prepare()
    with pytest.raises(ValueError, match="value must be a number"):
        ConditionEvaluator(
            {
                "condition_id": "c2",
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "operator": ">=",
                "value": "abc",
                "product": "AAPL",
                "contract_id": 1,
            }
        ).prepare()


def test_condition_evaluator_prepare_metric_trigger_window_matrix() -> None:
    for metric in ALL_METRICS:
        allowed_rules = resolve_metric_allowed_rules(metric)
        allowed_windows = resolve_metric_allowed_windows(metric)
        assert allowed_rules
        assert allowed_windows
        for trigger_mode, operator in sorted(allowed_rules):
            for evaluation_window in sorted(allowed_windows):
                payload = _build_condition_payload(
                    metric=metric,
                    trigger_mode=trigger_mode,
                    operator=operator,
                    evaluation_window=evaluation_window,
                )
                try:
                    policy = resolve_trigger_window_policy(trigger_mode, evaluation_window)
                except ValueError:
                    with pytest.raises(ValueError, match="does not allow evaluation_window"):
                        ConditionEvaluator(payload).prepare()
                    continue

                condition_evaluator = ConditionEvaluator(payload)
                condition_evaluator.prepare()
                prepared = condition_evaluator.prepared
                assert prepared.metric == metric
                assert prepared.trigger_mode == policy.trigger_mode
                assert prepared.evaluation_window == policy.evaluation_window
                assert prepared.requirement.contracts
                assert prepared.requirement.contracts[0].base_bar == policy.base_bar
                assert prepared.requirement.contracts[0].required_points >= 1
                if metric in PAIR_METRICS:
                    assert len(prepared.requirement.contracts) == 2
                    assert prepared.requirement.contracts[1].contract_id is not None
                else:
                    assert len(prepared.requirement.contracts) == 1


def test_condition_evaluator_prepare_rejects_disallowed_metric_rule_matrix() -> None:
    for metric in ALL_METRICS:
        allowed_rules = resolve_metric_allowed_rules(metric)
        allowed_windows = resolve_metric_allowed_windows(metric)
        for trigger_mode in SUPPORTED_TRIGGER_MODES:
            for operator in ALL_OPERATORS:
                for evaluation_window in ALL_WINDOWS:
                    payload = _build_condition_payload(
                        metric=metric,
                        trigger_mode=trigger_mode,
                        operator=operator,
                        evaluation_window=evaluation_window,
                    )
                    rule_ok = (trigger_mode, operator) in allowed_rules
                    window_ok = evaluation_window in allowed_windows
                    try:
                        resolve_trigger_window_policy(trigger_mode, evaluation_window)
                        trigger_window_ok = True
                    except ValueError:
                        trigger_window_ok = False
                    should_succeed = rule_ok and window_ok and trigger_window_ok
                    if should_succeed:
                        continue
                    with pytest.raises(ValueError):
                        ConditionEvaluator(payload).prepare()
