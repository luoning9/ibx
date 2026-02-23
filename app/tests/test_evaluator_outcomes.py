from __future__ import annotations

import os
from datetime import datetime, timezone

from app.evaluator import evaluate_strategy


UTC = timezone.utc


def _base_row(conditions_json: str, *, condition_logic: str = "AND") -> dict[str, object]:
    return {
        "conditions_json": conditions_json,
        "condition_logic": condition_logic,
    }


def test_evaluator_no_conditions_configured() -> None:
    result = evaluate_strategy(_base_row("[]"), now=datetime.now(UTC))
    assert result.outcome == "no_conditions_configured"
    assert result.condition_met is False


def test_evaluator_gateway_not_work() -> None:
    old = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_GATEWAY_READY"] = "0"
    try:
        result = evaluate_strategy(
            _base_row(
                '[{"condition_id":"c1","metric":"PRICE","operator":">=","value":1.0,"contract_id":1}]',
            ),
            now=datetime.now(UTC),
        )
        assert result.outcome == "gateway_not_work"
        assert result.condition_met is False
    finally:
        if old is None:
            os.environ.pop("IBX_GATEWAY_READY", None)
        else:
            os.environ["IBX_GATEWAY_READY"] = old


def test_evaluator_waiting_for_market_data() -> None:
    result = evaluate_strategy(
        _base_row(
            '[{"condition_id":"c1","metric":"PRICE","operator":">=","value":1.0,"contract_id":1}]',
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "waiting_for_market_data"
    assert result.condition_met is False


def test_evaluator_evaluated() -> None:
    result = evaluate_strategy(
        _base_row(
            '[{"condition_id":"c1","metric":"PRICE","operator":">=","value":1.0,"observed_value":2.0,"contract_id":1}]',
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "evaluated"
    assert result.condition_met is True


def test_evaluator_or_true_even_when_other_condition_waiting() -> None:
    result = evaluate_strategy(
        _base_row(
            (
                "["
                '{"condition_id":"c1","metric":"PRICE","operator":">=","value":1.0,"observed_value":2.0,"contract_id":1},'
                '{"condition_id":"c2","metric":"PRICE","operator":">=","value":1.0,"contract_id":2}'
                "]"
            ),
            condition_logic="OR",
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "evaluated"
    assert result.condition_met is True
    assert result.decision_reason == "conditions_met"


def test_evaluator_and_false_even_when_other_condition_waiting() -> None:
    result = evaluate_strategy(
        _base_row(
            (
                "["
                '{"condition_id":"c1","metric":"PRICE","operator":">=","value":10.0,"observed_value":2.0,"contract_id":1},'
                '{"condition_id":"c2","metric":"PRICE","operator":">=","value":1.0,"contract_id":2}'
                "]"
            ),
            condition_logic="AND",
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "evaluated"
    assert result.condition_met is False
    assert result.decision_reason == "conditions_not_met"


def test_evaluator_condition_config_invalid_when_contract_id_missing() -> None:
    result = evaluate_strategy(
        _base_row(
            '[{"condition_id":"c1","metric":"PRICE","operator":">=","value":1.0}]',
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "condition_config_invalid"
    assert result.condition_met is False
    assert result.decision_reason == "condition_config_invalid"


def test_evaluator_condition_config_invalid_when_threshold_missing() -> None:
    result = evaluate_strategy(
        _base_row(
            '[{"condition_id":"c1","metric":"PRICE","operator":">=","contract_id":1}]',
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "condition_config_invalid"
    assert result.condition_met is False
    assert result.decision_reason == "condition_config_invalid"


def test_evaluator_condition_config_invalid() -> None:
    result = evaluate_strategy(
        _base_row(
            '[{"condition_id":"c1","operator":">=","value":1.0,"contract_id":1}]',
        ),
        now=datetime.now(UTC),
    )
    assert result.outcome == "condition_config_invalid"
    assert result.condition_met is False
    assert result.decision_reason == "condition_config_invalid"
