from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Protocol

from .config import PROJECT_ROOT, infer_ib_api_port, load_app_config, resolve_ib_client_id
from .market_config import resolve_market_profile


UTC = timezone.utc
DEFAULT_BROKER_DATA_FIXTURE_PATH = PROJECT_ROOT / "conf" / "fixtures" / "broker_data.sample.json"
_IB_REQUEST_LOCK = Lock()


class IBDataServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountPosition:
    account: str
    contract_id: int | None
    symbol: str
    local_symbol: str
    sec_type: str
    currency: str
    exchange: str
    position: float
    market_price: float
    market_value: float
    average_cost: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass(frozen=True)
class AccountSnapshot:
    account_code: str | None
    fetched_at: datetime
    values: dict[str, str]
    value_currencies: dict[str, str]
    values_float: dict[str, float]
    positions: list[AccountPosition]


class BrokerDataProvider(Protocol):
    def resolve_contract_id(
        self,
        *,
        code: str,
        market: str,
        contract_month: str | None = None,
    ) -> int: ...

    def get_account_snapshot(self, *, account_code: str | None = None) -> AccountSnapshot: ...


def _normalize_account(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if normalized else None


def _parse_iso_datetime_or_now(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


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
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _parse_contract_month(value: str | None) -> tuple[int, int, int] | None:
    raw = str(value or "").strip()
    if len(raw) < 6 or not raw[:6].isdigit():
        return None
    year = int(raw[:4])
    month = int(raw[4:6])
    if month < 1 or month > 12:
        return None
    day = 1
    if len(raw) >= 8 and raw[:8].isdigit():
        parsed_day = int(raw[6:8])
        if 1 <= parsed_day <= 31:
            day = parsed_day
    return (year, month, day)


def _pick_front_future_contract(details: list[Any], *, now: datetime) -> Any | None:
    today_key = (now.year, now.month, now.day)
    parsed: list[tuple[tuple[int, int, int], Any]] = []
    for item in details:
        contract = getattr(item, "contract", None)
        if contract is None:
            continue
        expiry = _parse_contract_month(getattr(contract, "lastTradeDateOrContractMonth", None))
        if expiry is None:
            continue
        parsed.append((expiry, contract))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    for expiry, contract in parsed:
        if expiry >= today_key:
            return contract
    return parsed[0][1]


def _default_contract_builder(
    *,
    sec_type: str,
    code: str,
    exchange: str,
    currency: str,
    contract_month: str | None,
) -> Any:
    try:
        from ib_insync import Future, Stock
    except ModuleNotFoundError as exc:
        raise IBDataServiceError(
            "Missing dependency: ib_insync. Install with: pip install ib_insync"
        ) from exc

    sec_type_key = sec_type.strip().upper()
    if sec_type_key == "STK":
        return Stock(symbol=code, exchange=exchange, currency=currency)
    if sec_type_key == "FUT":
        kwargs: dict[str, Any] = {
            "symbol": code,
            "exchange": exchange,
            "currency": currency,
        }
        if contract_month:
            kwargs["lastTradeDateOrContractMonth"] = contract_month
        return Future(**kwargs)
    raise ValueError(f"unsupported sec_type for IB contract builder: {sec_type}")


class IBDataService:
    def __init__(
        self,
        *,
        ib: Any | None = None,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        timeout_seconds: float | None = None,
        account_code: str | None = None,
        trading_mode: str | None = None,
        readonly: bool = True,
        contract_builder: Callable[..., Any] | None = None,
    ) -> None:
        cfg = load_app_config().ib_gateway
        mode = str(trading_mode or cfg.trading_mode).strip().lower()
        self.host = str(host or cfg.host)
        self.port = int(port if port is not None else infer_ib_api_port(mode))
        self.client_id = int(client_id if client_id is not None else resolve_ib_client_id("broker_data"))
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else cfg.timeout_seconds)
        self.default_account_code = _normalize_account(account_code or cfg.account_code)
        self.readonly = bool(readonly)
        self._ib = ib
        self._contract_builder = contract_builder or _default_contract_builder

    def _ensure_ib(self) -> Any:
        _ensure_thread_event_loop()
        if self._ib is not None:
            return self._ib
        try:
            from ib_insync import IB
        except ModuleNotFoundError as exc:
            raise IBDataServiceError(
                "Missing dependency: ib_insync. Install with: pip install ib_insync"
            ) from exc
        self._ib = IB()
        return self._ib

    def connect(self) -> None:
        ib = self._ensure_ib()
        if bool(getattr(ib, "isConnected", lambda: False)()):
            return
        try:
            ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=self.timeout_seconds,
                readonly=self.readonly,
            )
        except Exception as exc:  # pragma: no cover - exercised via store/api path
            try:
                ib.disconnect()
            except Exception:
                pass
            raise IBDataServiceError(
                "failed to connect IB gateway"
                f" host={self.host} port={self.port} client_id={self.client_id}: {exc or ''}"
            ) from exc

    def disconnect(self) -> None:
        with _IB_REQUEST_LOCK:
            ib = self._ensure_ib()
            try:
                ib.disconnect()
            except Exception:
                pass

    def resolve_contract_id(
        self,
        *,
        code: str,
        market: str,
        contract_month: str | None = None,
    ) -> int:
        with _IB_REQUEST_LOCK:
            normalized_code = str(code).strip().upper()
            if not normalized_code:
                raise ValueError("code is required")
            normalized_month = str(contract_month or "").strip() or None

            profile = resolve_market_profile(market, None)
            self.connect()
            ib = self._ensure_ib()

            if profile.sec_type == "FUT" and normalized_month is None:
                probe = self._contract_builder(
                    sec_type=profile.sec_type,
                    code=normalized_code,
                    exchange=profile.exchange,
                    currency=profile.currency,
                    contract_month=None,
                )
                details = list(ib.reqContractDetails(probe))
                front = _pick_front_future_contract(details, now=datetime.now(UTC))
                if front is not None:
                    con_id = _to_int_or_none(getattr(front, "conId", None))
                    if con_id is not None:
                        return con_id

            candidate = self._contract_builder(
                sec_type=profile.sec_type,
                code=normalized_code,
                exchange=profile.exchange,
                currency=profile.currency,
                contract_month=normalized_month,
            )
            qualified = list(ib.qualifyContracts(candidate))
            if not qualified:
                raise IBDataServiceError(
                    f"failed to resolve contract_id for market={profile.market}, code={normalized_code}"
                )
            con_id = _to_int_or_none(getattr(qualified[0], "conId", None))
            if con_id is None:
                raise IBDataServiceError(
                    f"resolved contract has invalid conId for market={profile.market}, code={normalized_code}"
                )
            return con_id

    def get_account_snapshot(self, *, account_code: str | None = None) -> AccountSnapshot:
        with _IB_REQUEST_LOCK:
            target_account = _normalize_account(account_code) or self.default_account_code
            self.connect()
            ib = self._ensure_ib()

            summary_items = list(ib.accountSummary())
            portfolio_items = list(ib.portfolio())

            if target_account is None:
                if summary_items:
                    target_account = _normalize_account(getattr(summary_items[0], "account", None))
                elif portfolio_items:
                    target_account = _normalize_account(getattr(portfolio_items[0], "account", None))

            filtered_summary = summary_items
            filtered_portfolio = portfolio_items
            if target_account:
                filtered_summary = [
                    item for item in summary_items if _normalize_account(getattr(item, "account", None)) == target_account
                ]
                filtered_portfolio = [
                    item for item in portfolio_items if _normalize_account(getattr(item, "account", None)) == target_account
                ]

            values: dict[str, str] = {}
            value_currencies: dict[str, str] = {}
            values_float: dict[str, float] = {}
            for item in filtered_summary:
                tag = str(getattr(item, "tag", "")).strip()
                if not tag:
                    continue
                value = str(getattr(item, "value", "")).strip()
                currency = str(getattr(item, "currency", "")).strip()
                values[tag] = value
                if currency:
                    value_currencies[tag] = currency
                parsed = _to_float_or_none(value)
                if parsed is not None:
                    values_float[tag] = parsed

            positions: list[AccountPosition] = []
            for item in filtered_portfolio:
                contract = getattr(item, "contract", None)
                if contract is None:
                    continue
                positions.append(
                    AccountPosition(
                        account=str(getattr(item, "account", "")).strip(),
                        contract_id=_to_int_or_none(getattr(contract, "conId", None)),
                        symbol=str(getattr(contract, "symbol", "")).strip(),
                        local_symbol=str(getattr(contract, "localSymbol", "")).strip(),
                        sec_type=str(getattr(contract, "secType", "")).strip(),
                        currency=str(getattr(contract, "currency", "")).strip(),
                        exchange=str(getattr(contract, "exchange", "")).strip(),
                        position=float(getattr(item, "position", 0.0)),
                        market_price=float(getattr(item, "marketPrice", 0.0)),
                        market_value=float(getattr(item, "marketValue", 0.0)),
                        average_cost=float(getattr(item, "averageCost", 0.0)),
                        unrealized_pnl=float(getattr(item, "unrealizedPNL", 0.0)),
                        realized_pnl=float(getattr(item, "realizedPNL", 0.0)),
                    )
                )

            return AccountSnapshot(
                account_code=target_account,
                fetched_at=datetime.now(UTC),
                values=values,
                value_currencies=value_currencies,
                values_float=values_float,
                positions=positions,
            )

    def __enter__(self) -> IBDataService:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.disconnect()


class FixtureBrokerDataProvider:
    def __init__(self, *, fixture_path: str | Path | None = None) -> None:
        self._fixture_path = Path(fixture_path) if fixture_path is not None else DEFAULT_BROKER_DATA_FIXTURE_PATH
        self._cache: dict[str, Any] | None = None

    def _load_payload(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self._fixture_path.exists():
            raise IBDataServiceError(f"fixture data file not found: {self._fixture_path}")
        try:
            payload = json.loads(self._fixture_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IBDataServiceError(f"invalid fixture data JSON: {self._fixture_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise IBDataServiceError("fixture data root must be a JSON object")
        self._cache = payload
        return payload

    def resolve_contract_id(
        self,
        *,
        code: str,
        market: str,
        contract_month: str | None = None,
    ) -> int:
        normalized_code = str(code).strip().upper()
        normalized_market = str(market).strip().upper()
        normalized_month = str(contract_month or "").strip() or None
        if not normalized_code:
            raise ValueError("code is required")
        if not normalized_market:
            raise ValueError("market is required")

        payload = self._load_payload()
        contracts = payload.get("contracts")
        if not isinstance(contracts, list):
            raise IBDataServiceError("fixture data must contain `contracts` list")

        fallback_row: dict[str, Any] | None = None
        for item in contracts:
            row = item if isinstance(item, dict) else {}
            row_market = str(row.get("market", "")).strip().upper()
            row_code = str(row.get("code", "")).strip().upper()
            if row_market != normalized_market or row_code != normalized_code:
                continue
            row_month = str(row.get("contract_month", "")).strip() or None
            con_id = _to_int_or_none(row.get("contract_id"))
            if con_id is None:
                continue
            if normalized_month is not None:
                if row_month == normalized_month:
                    return con_id
                continue
            if row_month is None:
                return con_id
            if fallback_row is None:
                fallback_row = row

        if normalized_month is None and fallback_row is not None:
            con_id = _to_int_or_none(fallback_row.get("contract_id"))
            if con_id is not None:
                return con_id

        raise IBDataServiceError(
            f"fixture contract_id not found for market={normalized_market}, code={normalized_code}"
            + (f", contract_month={normalized_month}" if normalized_month else "")
        )

    def get_account_snapshot(self, *, account_code: str | None = None) -> AccountSnapshot:
        normalized_account = _normalize_account(account_code)
        payload = self._load_payload()
        snapshots = payload.get("account_snapshots")
        snapshot_payload: dict[str, Any]
        resolved_account = normalized_account

        if isinstance(snapshots, dict) and snapshots:
            chosen = None
            if normalized_account is not None:
                chosen = snapshots.get(normalized_account)
            if chosen is None:
                chosen = snapshots.get("default")
            if chosen is None:
                first_key = next(iter(snapshots.keys()))
                chosen = snapshots[first_key]
                if resolved_account is None and first_key != "default":
                    resolved_account = str(first_key)
            snapshot_payload = chosen if isinstance(chosen, dict) else {}
        else:
            snapshot_payload = payload.get("account_snapshot") if isinstance(payload.get("account_snapshot"), dict) else {}

        fetched_at = _parse_iso_datetime_or_now(snapshot_payload.get("fetched_at"))
        values_raw = snapshot_payload.get("values")
        values: dict[str, str] = {}
        if isinstance(values_raw, dict):
            for key, value in values_raw.items():
                tag = str(key).strip()
                if not tag:
                    continue
                values[tag] = str(value)

        value_currencies_raw = snapshot_payload.get("value_currencies")
        value_currencies: dict[str, str] = {}
        if isinstance(value_currencies_raw, dict):
            for key, value in value_currencies_raw.items():
                tag = str(key).strip()
                if not tag:
                    continue
                value_currencies[tag] = str(value)

        values_float_raw = snapshot_payload.get("values_float")
        values_float: dict[str, float] = {}
        if isinstance(values_float_raw, dict):
            for key, value in values_float_raw.items():
                tag = str(key).strip()
                parsed = _to_float_or_none(value)
                if not tag or parsed is None:
                    continue
                values_float[tag] = parsed
        else:
            for tag, value in values.items():
                parsed = _to_float_or_none(value)
                if parsed is not None:
                    values_float[tag] = parsed

        positions_raw = snapshot_payload.get("positions")
        positions: list[AccountPosition] = []
        if isinstance(positions_raw, list):
            for item in positions_raw:
                row = item if isinstance(item, dict) else {}
                row_account = str(row.get("account", resolved_account or "")).strip()
                positions.append(
                    AccountPosition(
                        account=row_account,
                        contract_id=_to_int_or_none(row.get("contract_id")),
                        symbol=str(row.get("symbol", "")).strip(),
                        local_symbol=str(row.get("local_symbol", "")).strip(),
                        sec_type=str(row.get("sec_type", "")).strip(),
                        currency=str(row.get("currency", "")).strip(),
                        exchange=str(row.get("exchange", "")).strip(),
                        position=float(_to_float_or_none(row.get("position")) or 0.0),
                        market_price=float(_to_float_or_none(row.get("market_price")) or 0.0),
                        market_value=float(_to_float_or_none(row.get("market_value")) or 0.0),
                        average_cost=float(_to_float_or_none(row.get("average_cost")) or 0.0),
                        unrealized_pnl=float(_to_float_or_none(row.get("unrealized_pnl")) or 0.0),
                        realized_pnl=float(_to_float_or_none(row.get("realized_pnl")) or 0.0),
                    )
                )

        return AccountSnapshot(
            account_code=resolved_account,
            fetched_at=fetched_at,
            values=values,
            value_currencies=value_currencies,
            values_float=values_float,
            positions=positions,
        )


def build_broker_data_provider_from_config(
    *,
    fixture_path: str | Path | None = None,
) -> BrokerDataProvider:
    cfg = load_app_config()
    if cfg.providers.broker_data == "fixture":
        return FixtureBrokerDataProvider(fixture_path=fixture_path)
    return IBDataService()
