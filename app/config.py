from __future__ import annotations

import os
import json
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_CONFIG_PATH = PROJECT_ROOT / "conf" / "app.toml"
DEFAULT_CONDITION_RULES_CONFIG_PATH = PROJECT_ROOT / "conf" / "condition_rules.json"
SUPPORTED_TRIGGER_MODES: tuple[str, ...] = (
    "LEVEL_INSTANT",
    "LEVEL_CONFIRM",
    "CROSS_UP_INSTANT",
    "CROSS_UP_CONFIRM",
    "CROSS_DOWN_INSTANT",
    "CROSS_DOWN_CONFIRM",
)


@dataclass(frozen=True)
class IBGatewayConfig:
    host: str
    paper_port: int
    live_port: int
    client_id: int
    timeout_seconds: float
    account_code: str
    trading_mode: str


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: str | None
    db_path: str | None
    log_path: str | None
    market_data_log_path: str | None
    market_cache_db_path: str | None
    market_config_path: str | None
    enable_live_trading: bool


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool
    monitor_interval_seconds: int
    threads: int
    queue_maxsize: int
    gateway_not_work_event_throttle_seconds: int
    waiting_for_market_data_event_throttle_seconds: int


@dataclass(frozen=True)
class ProvidersConfig:
    broker_data: str
    market_data: str


@dataclass(frozen=True)
class TriggerWindowPolicy:
    base_bar: str
    confirm_consecutive: int
    confirm_ratio: float
    include_partial_bar: bool = False
    missing_data_policy: str = "fail"


@dataclass(frozen=True)
class ResolvedTriggerWindowPolicy:
    trigger_mode: str
    evaluation_window: str
    base_bar: str
    confirm_consecutive: int
    confirm_ratio: float
    include_partial_bar: bool
    missing_data_policy: str


@dataclass(frozen=True)
class TriggerModeConfig:
    fallback: TriggerWindowPolicy
    mode_defaults: dict[str, TriggerWindowPolicy]
    windows: dict[str, dict[str, TriggerWindowPolicy]]

    def resolve(self, trigger_mode: str, evaluation_window: str) -> ResolvedTriggerWindowPolicy:
        raw_mode = str(trigger_mode or "").strip().upper()
        canonical_mode = raw_mode
        window = str(evaluation_window or "").strip().lower() or "1m"

        mode_window_policies = self.windows.get(canonical_mode, {})
        if mode_window_policies:
            policy = mode_window_policies.get(window)
            if policy is None:
                raise ValueError(
                    f"trigger_mode={canonical_mode} does not allow evaluation_window={window}"
                )
        else:
            policy = self.mode_defaults.get(canonical_mode, self.fallback)

        return ResolvedTriggerWindowPolicy(
            trigger_mode=canonical_mode,
            evaluation_window=window,
            base_bar=policy.base_bar,
            confirm_consecutive=policy.confirm_consecutive,
            confirm_ratio=policy.confirm_ratio,
            include_partial_bar=policy.include_partial_bar,
            missing_data_policy=policy.missing_data_policy,
        )


@dataclass(frozen=True)
class MetricRuleConfig:
    allowed_rules: dict[str, set[tuple[str, str]]]
    allowed_windows: dict[str, set[str]]

    def resolve_rules(self, metric: str) -> set[tuple[str, str]]:
        key = str(metric or "").strip().upper()
        return set(self.allowed_rules.get(key, set()))

    def resolve_windows(self, metric: str) -> set[str]:
        key = str(metric or "").strip().upper()
        return set(self.allowed_windows.get(key, set()))


@dataclass(frozen=True)
class AppConfig:
    ib_gateway: IBGatewayConfig
    runtime: RuntimeConfig
    worker: WorkerConfig
    providers: ProvidersConfig
    trigger_mode: TriggerModeConfig
    metric_rules: MetricRuleConfig


def resolve_app_config_path() -> Path:
    env_path = os.getenv("IBX_APP_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_APP_CONFIG_PATH


def clear_app_config_cache() -> None:
    load_app_config.cache_clear()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any, default: str) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return default


def _as_optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(parsed, minimum)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _as_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(parsed, minimum)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_trading_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"paper", "live"}:
        return normalized
    return "paper"


def _normalize_missing_data_policy(value: Any, default: str = "fail") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"fail", "skip", "carry_forward"}:
        return normalized
    return default


def _normalize_broker_data_provider(value: Any, default: str = "ib") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ib", "fixture"}:
        return normalized
    return default


def _normalize_market_data_provider(value: Any, default: str = "ib") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ib", "fixture"}:
        return normalized
    return default


def _load_condition_rules_config_raw() -> dict[str, Any]:
    path = DEFAULT_CONDITION_RULES_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid condition rules JSON: {path}: {exc}") from exc
    return _as_dict(payload)


def _default_trigger_mode_defaults() -> dict[str, TriggerWindowPolicy]:
    defaults = {
        "LEVEL_INSTANT": TriggerWindowPolicy(
            base_bar="1m",
            confirm_consecutive=1,
            confirm_ratio=1.0,
            include_partial_bar=True,
            missing_data_policy="fail",
        ),
        "LEVEL_CONFIRM": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=2, confirm_ratio=0.5),
        "CROSS_UP_INSTANT": TriggerWindowPolicy(
            base_bar="1m",
            confirm_consecutive=1,
            confirm_ratio=1.0,
            include_partial_bar=True,
            missing_data_policy="fail",
        ),
        "CROSS_UP_CONFIRM": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=2, confirm_ratio=0.5),
        "CROSS_DOWN_INSTANT": TriggerWindowPolicy(
            base_bar="1m",
            confirm_consecutive=1,
            confirm_ratio=1.0,
            include_partial_bar=True,
            missing_data_policy="fail",
        ),
        "CROSS_DOWN_CONFIRM": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=2, confirm_ratio=0.5),
    }
    return {mode: defaults[mode] for mode in SUPPORTED_TRIGGER_MODES}


def _default_trigger_mode_windows() -> dict[str, dict[str, TriggerWindowPolicy]]:
    instant_windows = {
        "1m": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=1, confirm_ratio=1.0),
        "5m": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=1, confirm_ratio=1.0),
        "30m": TriggerWindowPolicy(base_bar="5m", confirm_consecutive=1, confirm_ratio=1.0),
        "1h": TriggerWindowPolicy(base_bar="5m", confirm_consecutive=1, confirm_ratio=1.0),
    }
    confirm_windows = {
        "5m": TriggerWindowPolicy(base_bar="1m", confirm_consecutive=4, confirm_ratio=0.8),
        "30m": TriggerWindowPolicy(base_bar="5m", confirm_consecutive=2, confirm_ratio=0.5),
        "1h": TriggerWindowPolicy(base_bar="5m", confirm_consecutive=2, confirm_ratio=0.5),
        "2h": TriggerWindowPolicy(base_bar="15m", confirm_consecutive=2, confirm_ratio=0.5),
        "4h": TriggerWindowPolicy(base_bar="15m", confirm_consecutive=2, confirm_ratio=0.5),
        "1d": TriggerWindowPolicy(base_bar="1h", confirm_consecutive=2, confirm_ratio=0.5),
        "2d": TriggerWindowPolicy(base_bar="1h", confirm_consecutive=2, confirm_ratio=0.5),
    }
    windows = {
        "LEVEL_INSTANT": dict(instant_windows),
        "LEVEL_CONFIRM": dict(confirm_windows),
        "CROSS_UP_INSTANT": dict(instant_windows),
        "CROSS_UP_CONFIRM": dict(confirm_windows),
        "CROSS_DOWN_INSTANT": dict(instant_windows),
        "CROSS_DOWN_CONFIRM": dict(confirm_windows),
    }
    return {mode: dict(windows[mode]) for mode in SUPPORTED_TRIGGER_MODES}


def _build_trigger_mode_config(raw: dict[str, Any]) -> TriggerModeConfig:
    default_fallback = TriggerWindowPolicy(base_bar="1m", confirm_consecutive=1, confirm_ratio=1.0)
    fallback = TriggerWindowPolicy(
        base_bar=_as_str(raw.get("fallback_base_bar"), default_fallback.base_bar),
        confirm_consecutive=_as_int(
            raw.get("fallback_confirm_consecutive"),
            default_fallback.confirm_consecutive,
            minimum=1,
            maximum=1000,
        ),
        confirm_ratio=_as_float(
            raw.get("fallback_confirm_ratio"),
            default_fallback.confirm_ratio,
            minimum=0.0,
            maximum=1.0,
        ),
        include_partial_bar=_as_bool(
            raw.get("fallback_include_partial_bar"),
            default_fallback.include_partial_bar,
        ),
        missing_data_policy=_normalize_missing_data_policy(
            raw.get("fallback_missing_data_policy"),
            default_fallback.missing_data_policy,
        ),
    )

    mode_defaults = _default_trigger_mode_defaults()
    mode_windows = _default_trigger_mode_windows()

    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, list):
        for profile_item in raw_profiles:
            profile = _as_dict(profile_item)
            raw_modes = profile.get("trigger_modes")
            if isinstance(raw_modes, list):
                trigger_modes = [str(mode).strip().upper() for mode in raw_modes if str(mode).strip()]
            elif isinstance(raw_modes, str):
                normalized = raw_modes.strip().upper()
                trigger_modes = [normalized] if normalized else []
            else:
                trigger_modes = []
            if not trigger_modes:
                continue

            profile_default = TriggerWindowPolicy(
                base_bar=_as_str(profile.get("base_bar"), fallback.base_bar),
                confirm_consecutive=_as_int(
                    profile.get("confirm_consecutive"),
                    fallback.confirm_consecutive,
                    minimum=1,
                    maximum=1000,
                ),
                confirm_ratio=_as_float(
                    profile.get("confirm_ratio"),
                    fallback.confirm_ratio,
                    minimum=0.0,
                    maximum=1.0,
                ),
                include_partial_bar=_as_bool(
                    profile.get("include_partial_bar"),
                    fallback.include_partial_bar,
                ),
                missing_data_policy=_normalize_missing_data_policy(
                    profile.get("missing_data_policy"),
                    fallback.missing_data_policy,
                ),
            )

            parsed_windows: dict[str, TriggerWindowPolicy] = {}
            raw_windows = _as_dict(profile.get("windows"))
            for window_name, window_spec in raw_windows.items():
                window = str(window_name).strip().lower()
                if not window:
                    continue
                window_spec_dict = _as_dict(window_spec)
                parsed_windows[window] = TriggerWindowPolicy(
                    base_bar=_as_str(window_spec_dict.get("base_bar"), profile_default.base_bar),
                    confirm_consecutive=_as_int(
                        window_spec_dict.get("confirm_consecutive"),
                        profile_default.confirm_consecutive,
                        minimum=1,
                        maximum=1000,
                    ),
                    confirm_ratio=_as_float(
                        window_spec_dict.get("confirm_ratio"),
                        profile_default.confirm_ratio,
                        minimum=0.0,
                        maximum=1.0,
                    ),
                    include_partial_bar=_as_bool(
                        window_spec_dict.get("include_partial_bar"),
                        profile_default.include_partial_bar,
                    ),
                    missing_data_policy=_normalize_missing_data_policy(
                        window_spec_dict.get("missing_data_policy"),
                        profile_default.missing_data_policy,
                    ),
                )

            for canonical_mode in trigger_modes:
                mode_defaults[canonical_mode] = profile_default
                if parsed_windows:
                    mode_windows[canonical_mode] = dict(parsed_windows)

    return TriggerModeConfig(
        fallback=fallback,
        mode_defaults=mode_defaults,
        windows=mode_windows,
    )


def _default_metric_allowed_rules() -> dict[str, set[tuple[str, str]]]:
    return {
        "PRICE": {
            ("LEVEL_INSTANT", ">="),
            ("LEVEL_INSTANT", "<="),
            ("LEVEL_CONFIRM", ">="),
            ("LEVEL_CONFIRM", "<="),
            ("CROSS_UP_INSTANT", ">="),
            ("CROSS_UP_CONFIRM", ">="),
            ("CROSS_DOWN_INSTANT", "<="),
            ("CROSS_DOWN_CONFIRM", "<="),
        },
        "DRAWDOWN_PCT": {("LEVEL_INSTANT", ">="), ("LEVEL_CONFIRM", ">=")},
        "RALLY_PCT": {("LEVEL_INSTANT", ">="), ("LEVEL_CONFIRM", ">=")},
        "VOLUME_RATIO": {
            ("LEVEL_CONFIRM", ">="),
            ("LEVEL_CONFIRM", "<="),
        },
        "AMOUNT_RATIO": {
            ("LEVEL_CONFIRM", ">="),
            ("LEVEL_CONFIRM", "<="),
        },
        "SPREAD": {
            ("LEVEL_INSTANT", ">="),
            ("LEVEL_INSTANT", "<="),
            ("LEVEL_CONFIRM", ">="),
            ("LEVEL_CONFIRM", "<="),
            ("CROSS_UP_INSTANT", ">="),
            ("CROSS_UP_CONFIRM", ">="),
            ("CROSS_DOWN_INSTANT", "<="),
            ("CROSS_DOWN_CONFIRM", "<="),
        },
    }


def _default_metric_allowed_windows() -> dict[str, set[str]]:
    return {
        "PRICE": {"1m", "5m", "30m", "1h"},
        "DRAWDOWN_PCT": {"1m", "5m", "30m", "1h"},
        "RALLY_PCT": {"1m", "5m", "30m", "1h"},
        "SPREAD": {"1m", "5m", "30m", "1h"},
        "VOLUME_RATIO": {"1h", "2h", "4h", "1d", "2d"},
        "AMOUNT_RATIO": {"1h", "2h", "4h", "1d", "2d"},
    }


def _build_metric_rule_config(raw: dict[str, Any]) -> MetricRuleConfig:
    merged_rules = _default_metric_allowed_rules()
    merged_windows = _default_metric_allowed_windows()

    raw_windows = _as_dict(raw.get("allowed_windows"))
    for metric_name, windows in raw_windows.items():
        metric_key = str(metric_name).strip().upper()
        if not metric_key:
            continue
        parsed_windows: set[str] = set()
        if isinstance(windows, list):
            for item in windows:
                window = str(item or "").strip().lower()
                if window:
                    parsed_windows.add(window)
        if parsed_windows:
            merged_windows[metric_key] = parsed_windows

    raw_rules = _as_dict(raw.get("allowed_rules"))
    for metric_name, pairs in raw_rules.items():
        metric_key = str(metric_name).strip().upper()
        if not metric_key:
            continue
        parsed_pairs: set[tuple[str, str]] = set()
        if isinstance(pairs, list):
            for item in pairs:
                item_dict = _as_dict(item)
                trigger_mode = str(item_dict.get("trigger_mode", "")).strip().upper()
                operator = str(item_dict.get("operator", "")).strip()
                if not trigger_mode:
                    continue
                if operator not in {">=", "<="}:
                    continue
                parsed_pairs.add((trigger_mode, operator))
        if parsed_pairs:
            merged_rules[metric_key] = parsed_pairs
    return MetricRuleConfig(allowed_rules=merged_rules, allowed_windows=merged_windows)


@lru_cache(maxsize=1)
def load_app_config() -> AppConfig:
    path = resolve_app_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = _as_dict(tomllib.loads(path.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"invalid app config TOML: {path}: {exc}") from exc

    ib_raw = _as_dict(raw.get("ib_gateway"))
    runtime_raw = _as_dict(raw.get("runtime"))
    worker_raw = _as_dict(raw.get("worker"))
    providers_raw = _as_dict(raw.get("providers"))
    condition_rules_raw = _load_condition_rules_config_raw()
    trigger_mode_raw = _as_dict(condition_rules_raw.get("trigger_mode_profiles"))
    metric_rules_raw = _as_dict(condition_rules_raw.get("metric_trigger_operator_rules"))

    ib = IBGatewayConfig(
        host=_as_str(ib_raw.get("host"), "127.0.0.1"),
        paper_port=_as_int(ib_raw.get("paper_port"), 4002, minimum=1, maximum=65535),
        live_port=_as_int(ib_raw.get("live_port"), 4001, minimum=1, maximum=65535),
        client_id=_as_int(ib_raw.get("client_id"), 99, minimum=1),
        timeout_seconds=_as_float(ib_raw.get("timeout_seconds"), 5.0, minimum=0.1),
        account_code=_as_str(ib_raw.get("account_code"), ""),
        trading_mode=_normalize_trading_mode(_as_str(ib_raw.get("trading_mode"), "paper")),
    )

    runtime = RuntimeConfig(
        data_dir=_as_optional_str(runtime_raw.get("data_dir")),
        db_path=_as_optional_str(runtime_raw.get("db_path")),
        log_path=_as_optional_str(runtime_raw.get("log_path")),
        market_data_log_path=_as_optional_str(runtime_raw.get("market_data_log_path")),
        market_cache_db_path=_as_optional_str(runtime_raw.get("market_cache_db_path")),
        market_config_path=_as_optional_str(runtime_raw.get("market_config_path")),
        enable_live_trading=_as_bool(runtime_raw.get("enable_live_trading"), False),
    )

    worker = WorkerConfig(
        enabled=_as_bool(worker_raw.get("enabled"), False),
        monitor_interval_seconds=_as_int(worker_raw.get("monitor_interval_seconds"), 60, minimum=20, maximum=300),
        threads=_as_int(worker_raw.get("threads"), 2, minimum=1, maximum=32),
        queue_maxsize=_as_int(worker_raw.get("queue_maxsize"), 4096, minimum=64, maximum=100000),
        gateway_not_work_event_throttle_seconds=_as_int(
            worker_raw.get("gateway_not_work_event_throttle_seconds"),
            300,
            minimum=10,
            maximum=86400,
        ),
        waiting_for_market_data_event_throttle_seconds=_as_int(
            worker_raw.get("waiting_for_market_data_event_throttle_seconds"),
            120,
            minimum=10,
            maximum=86400,
        ),
    )
    providers = ProvidersConfig(
        broker_data=_normalize_broker_data_provider(
            providers_raw.get("broker_data"),
            "ib",
        ),
        market_data=_normalize_market_data_provider(
            providers_raw.get("market_data"),
            "ib",
        ),
    )
    trigger_mode = _build_trigger_mode_config(trigger_mode_raw)
    metric_rules = _build_metric_rule_config(metric_rules_raw)

    return AppConfig(
        ib_gateway=ib,
        runtime=runtime,
        worker=worker,
        providers=providers,
        trigger_mode=trigger_mode,
        metric_rules=metric_rules,
    )


def infer_ib_api_port(trading_mode: str | None = None) -> int:
    cfg = load_app_config().ib_gateway
    mode = _normalize_trading_mode(str(trading_mode or cfg.trading_mode))
    if mode == "live":
        return cfg.live_port
    return cfg.paper_port


def resolve_trigger_window_policy(
    trigger_mode: str,
    evaluation_window: str,
    *,
    config: AppConfig | None = None,
) -> ResolvedTriggerWindowPolicy:
    cfg = config or load_app_config()
    return cfg.trigger_mode.resolve(trigger_mode, evaluation_window)


def resolve_metric_allowed_rules(
    metric: str,
    *,
    config: AppConfig | None = None,
) -> set[tuple[str, str]]:
    cfg = config or load_app_config()
    return cfg.metric_rules.resolve_rules(metric)


def resolve_metric_allowed_windows(
    metric: str,
    *,
    config: AppConfig | None = None,
) -> set[str]:
    cfg = config or load_app_config()
    return cfg.metric_rules.resolve_windows(metric)


def export_condition_rules(
    *,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    cfg = config or load_app_config()
    window_order = {"1m": 1, "5m": 2, "30m": 3, "1h": 4, "2h": 5, "4h": 6, "1d": 7, "2d": 8}
    trigger_mode_order = {
        "LEVEL_INSTANT": 1,
        "LEVEL_CONFIRM": 2,
        "CROSS_UP_INSTANT": 3,
        "CROSS_UP_CONFIRM": 4,
        "CROSS_DOWN_INSTANT": 5,
        "CROSS_DOWN_CONFIRM": 6,
    }

    trigger_mode_windows: dict[str, dict[str, Any]] = {}
    for trigger_mode, windows in cfg.trigger_mode.windows.items():
        trigger_mode_windows[trigger_mode] = {}
        for evaluation_window, policy in sorted(
            windows.items(),
            key=lambda item: (window_order.get(item[0], 999), item[0]),
        ):
            trigger_mode_windows[trigger_mode][evaluation_window] = {
                "base_bar": policy.base_bar,
                "confirm_consecutive": policy.confirm_consecutive,
                "confirm_ratio": policy.confirm_ratio,
                "include_partial_bar": policy.include_partial_bar,
                "missing_data_policy": policy.missing_data_policy,
            }

    metric_allowed_windows: dict[str, list[str]] = {}
    for metric, windows in cfg.metric_rules.allowed_windows.items():
        metric_allowed_windows[metric] = sorted(
            windows,
            key=lambda item: (window_order.get(item, 999), item),
        )

    metric_allowed_rules: dict[str, list[dict[str, str]]] = {}
    for metric, pairs in cfg.metric_rules.allowed_rules.items():
        metric_allowed_rules[metric] = sorted(
            [{"trigger_mode": mode, "operator": operator} for mode, operator in pairs],
            key=lambda item: (
                trigger_mode_order.get(item["trigger_mode"], 999),
                item["trigger_mode"],
                item["operator"],
            ),
        )

    return {
        "trigger_mode_windows": trigger_mode_windows,
        "metric_trigger_operator_rules": {
            "allowed_windows": metric_allowed_windows,
            "allowed_rules": metric_allowed_rules,
        },
    }
