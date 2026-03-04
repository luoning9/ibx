from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .config import (
    SUPPORTED_IB_ROLES,
    load_app_config,
    resolve_ib_client_id,
    resolve_ib_role_port,
    resolve_ib_role_readonly,
)
from .ib_compat import INSTALL_HINT, require_ib_attr
from .ib_session_manager import IBSessionManager, get_ib_session_manager
from .market_config import resolve_market_profile
from .market_data import HistoricalBar, TradingCalendarResult, TradingCalendarSession


UTC = timezone.utc


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _ib_utc(dt: datetime) -> str:
    return _to_utc(dt).strftime("%Y%m%d %H:%M:%S UTC")


def _parse_bar_size_delta(bar_size: str) -> timedelta | None:
    text = bar_size.strip().lower()
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        amount = int(parts[0])
    except ValueError:
        return None
    unit = parts[1]
    if unit in {"sec", "secs", "second", "seconds"}:
        return timedelta(seconds=amount)
    if unit in {"min", "mins", "minute", "minutes"}:
        return timedelta(minutes=amount)
    if unit in {"hour", "hours"}:
        return timedelta(hours=amount)
    if unit in {"day", "days"}:
        return timedelta(days=amount)
    return None


def _duration_str(start_time: datetime, end_time: datetime, *, bar_delta: timedelta | None) -> str:
    seconds = max(1, math.ceil((_to_utc(end_time) - _to_utc(start_time)).total_seconds()))
    if bar_delta is not None and bar_delta.total_seconds() > 0:
        bar_seconds = int(math.ceil(bar_delta.total_seconds()))
        # IB rejects too-short durations for a given bar size (e.g. 20 S with 1 min bars).
        seconds = max(seconds, bar_seconds)
        # Add one extra bar as a left-side buffer to reduce "just-closed bar not visible yet"
        # windows that often surface as HMDS 162 (query returned no data).
        seconds += bar_seconds

    if bar_delta is not None and bar_delta >= timedelta(hours=1):
        days = max(1, math.ceil(seconds / 86400))
        return f"{days} D"

    if seconds <= 86400:
        return f"{seconds} S"
    days = max(1, math.ceil(seconds / 86400))
    return f"{days} D"


def _coerce_ib_bar_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return _to_utc(raw)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise RuntimeError("empty bar date string")
        if len(text) == 8 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d")
            return parsed.replace(tzinfo=UTC)
        normalized = " ".join(text.split())
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return _to_utc(parsed)
        except ValueError:
            pass
        try:
            parsed = datetime.strptime(normalized, "%Y%m%d %H:%M:%S")
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
        raise RuntimeError(f"unsupported bar date string format: {text!r}")
    raise RuntimeError(f"unexpected bar date type: {type(raw)!r}")


def _parse_schedule_dt(value: Any, *, fallback_tz: Any) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%Y%m%d-%H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=fallback_tz)
        except ValueError:
            continue
    raise RuntimeError(f"unsupported schedule datetime format: {value!r}")


def _load_ib_contract_types() -> tuple[type[Any], type[Any]]:
    try:
        Future = require_ib_attr("Future")
        Stock = require_ib_attr("Stock")
    except ModuleNotFoundError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return Stock, Future


class IBSessionHistoricalFetcher:
    def __init__(
        self,
        *,
        use_role: str = "market_data",
        session_manager: IBSessionManager | None = None,
    ) -> None:
        cfg = load_app_config().ib_gateway
        normalized_role = str(use_role or "").strip().lower()
        if normalized_role not in SUPPORTED_IB_ROLES:
            normalized_role = "market_data"
        self.use_role = normalized_role
        self.host = str(cfg.host)
        self.port = int(resolve_ib_role_port(self.use_role))
        self.client_id = int(resolve_ib_client_id(self.use_role))
        self.timeout_seconds = float(cfg.timeout_seconds)
        self.readonly = bool(resolve_ib_role_readonly(self.use_role))
        self._session_manager = session_manager or get_ib_session_manager()
        self._contract_cache: dict[str, Any] = {}
        self._cache_lock = Lock()

    def fetch(
        self,
        *,
        contract: Mapping[str, Any] | str,
        start_time: datetime,
        end_time: datetime,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[HistoricalBar] | tuple[list[HistoricalBar], dict[str, Any]]:
        session = self._session_manager.get_session(role=self.use_role)

        def _fetch_with_ib(ib: Any) -> list[HistoricalBar] | tuple[list[HistoricalBar], dict[str, Any]]:
            ib_contract = self._resolve_contract_with_ib(ib, contract)
            bar_delta = _parse_bar_size_delta(bar_size)
            captured_errors: list[dict[str, Any]] = []

            def _on_error(
                req_id: Any,
                error_code: Any,
                error_message: Any,
                error_contract: Any = None,
                *args: Any,
            ) -> None:
                _ = args
                try:
                    normalized_req_id = int(req_id)
                except Exception:
                    normalized_req_id = -1
                try:
                    normalized_code = int(error_code)
                except Exception:
                    normalized_code = -1
                payload: dict[str, Any] = {
                    "req_id": normalized_req_id,
                    "code": normalized_code,
                    "message": str(error_message or ""),
                }
                contract_obj = error_contract if error_contract is not None else ib_contract
                symbol = str(
                    getattr(contract_obj, "localSymbol", "")
                    or getattr(contract_obj, "symbol", "")
                    or ""
                ).strip()
                exchange = str(getattr(contract_obj, "exchange", "") or "").strip()
                if symbol:
                    payload["symbol"] = symbol
                if exchange:
                    payload["exchange"] = exchange
                captured_errors.append(payload)

            error_event = getattr(ib, "errorEvent", None)
            subscribed = False
            if error_event is not None:
                try:
                    error_event += _on_error
                    subscribed = True
                except Exception:
                    subscribed = False

            try:
                bars = ib.reqHistoricalData(
                    ib_contract,  # type: ignore[arg-type]
                    endDateTime=_ib_utc(end_time),
                    durationStr=_duration_str(start_time, end_time, bar_delta=bar_delta),
                    barSizeSetting=bar_size,
                    whatToShow=what_to_show,
                    useRTH=use_rth,
                    formatDate=2,
                    keepUpToDate=False,
                )
            finally:
                if subscribed:
                    try:
                        error_event -= _on_error
                    except Exception:
                        pass

            out: list[HistoricalBar] = []
            for item in bars:
                out.append(
                    HistoricalBar(
                        ts=_coerce_ib_bar_ts(item.date),
                        open=float(item.open),
                        high=float(item.high),
                        low=float(item.low),
                        close=float(item.close),
                        volume=None if item.volume is None else float(item.volume),
                        wap=None if item.average is None else float(item.average),
                        count=None if item.barCount is None else int(item.barCount),
                    )
                )
            req_id_raw = getattr(bars, "reqId", None)
            req_id: int | None = None
            try:
                if req_id_raw is not None:
                    req_id = int(req_id_raw)
            except Exception:
                req_id = None

            errors = captured_errors
            if req_id is not None:
                errors = [item for item in captured_errors if int(item.get("req_id", -1)) in {req_id, -1}]
            if not errors:
                return out

            error_codes = sorted(
                {
                    int(item["code"])
                    for item in errors
                    if isinstance(item.get("code"), int)
                }
            )
            return out, {
                "ib_req_id": req_id,
                "ib_error_count": len(errors),
                "ib_error_codes": error_codes,
                "ib_errors": errors,
            }

        return session.run(_fetch_with_ib)

    def fetch_trading_calendar(
        self,
        *,
        contract_id: int,
        as_of_time: datetime,
        use_rth: bool,
    ) -> TradingCalendarResult:
        session = self._session_manager.get_session(role=self.use_role)

        normalized_as_of = _to_utc(as_of_time) if as_of_time.tzinfo is None else as_of_time
        local_tz = normalized_as_of.tzinfo or UTC
        local_day = normalized_as_of.astimezone(local_tz).date()
        next_local_day = local_day + timedelta(days=1)
        target_dates = {local_day, next_local_day}
        end_local = datetime.combine(next_local_day, time(23, 59, 59), tzinfo=local_tz)
        local_tz_name = str(getattr(local_tz, "key", "") or local_tz)

        def _fetch_with_ib(ib: Any) -> TradingCalendarResult:
            ib_contract = self._resolve_contract_id_with_ib(ib, int(contract_id))
            schedule = ib.reqHistoricalSchedule(
                ib_contract,  # type: ignore[arg-type]
                numDays=2,
                endDateTime=end_local,
                useRTH=bool(use_rth),
            )
            schedule_tz_name = str(getattr(schedule, "timeZone", "") or "UTC")
            try:
                schedule_tz = ZoneInfo(schedule_tz_name)
            except Exception:
                schedule_tz = UTC
                schedule_tz_name = "UTC"

            sessions: list[TradingCalendarSession] = []
            for row in list(getattr(schedule, "sessions", []) or []):
                ref_date = str(getattr(row, "refDate", "")).strip()
                start_raw = getattr(row, "startDateTime", "")
                end_raw = getattr(row, "endDateTime", "")
                if not start_raw or not end_raw:
                    continue
                start_exchange = _parse_schedule_dt(start_raw, fallback_tz=schedule_tz)
                end_exchange = _parse_schedule_dt(end_raw, fallback_tz=schedule_tz)
                start_local = start_exchange.astimezone(local_tz)
                end_local_row = end_exchange.astimezone(local_tz)
                # Keep only sessions touching local day/as_of and next local day.
                if (start_local.date() not in target_dates) and (end_local_row.date() not in target_dates):
                    continue
                sessions.append(
                    TradingCalendarSession(
                        ref_date=ref_date,
                        start_time=_to_utc(start_local),
                        end_time=_to_utc(end_local_row),
                    )
                )
            sessions.sort(key=lambda item: (item.start_time, item.end_time, item.ref_date))
            return TradingCalendarResult(
                sessions=sessions,
                meta={
                    "source": "IB",
                    "contract_id": int(contract_id),
                    "use_rth": bool(use_rth),
                    "as_of_time": _to_utc(normalized_as_of).isoformat().replace("+00:00", "Z"),
                    "local_timezone": local_tz_name,
                    "local_day": local_day.isoformat(),
                    "next_local_day": next_local_day.isoformat(),
                    "schedule_timezone": schedule_tz_name,
                },
            )

        return session.run(_fetch_with_ib)

    def _resolve_contract_id_with_ib(self, ib: Any, contract_id: int) -> Any:
        if int(contract_id) <= 0:
            raise ValueError("contract_id must be positive")
        cache_key = f"CONID|{int(contract_id)}"
        with self._cache_lock:
            cached = self._contract_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            Contract = require_ib_attr("Contract")
        except ModuleNotFoundError as exc:
            raise RuntimeError(INSTALL_HINT) from exc

        probe = Contract(conId=int(contract_id))
        resolved: Any | None = None
        details = ib.reqContractDetails(probe)
        if details:
            resolved = details[0].contract
        if resolved is None:
            qualified = ib.qualifyContracts(probe)
            if qualified:
                resolved = qualified[0]
        if resolved is None:
            raise RuntimeError(f"failed to resolve contract by contract_id={int(contract_id)}")

        with self._cache_lock:
            self._contract_cache[cache_key] = resolved
        return resolved

    def _resolve_contract_with_ib(self, ib: Any, contract: Mapping[str, Any] | str) -> Any:
        if isinstance(contract, str):
            payload = {"market": "US_STOCK", "code": contract}
        else:
            payload = dict(contract)

        code = str(payload.get("code", "")).strip().upper()
        if not code:
            raise ValueError("contract.code is required")
        market = str(payload.get("market", "US_STOCK")).strip().upper() or "US_STOCK"
        contract_month = str(payload.get("contract_month", "")).strip()

        cache_key = f"{market}|{code}|{contract_month}"
        with self._cache_lock:
            cached = self._contract_cache.get(cache_key)
        if cached is not None:
            return cached

        profile = resolve_market_profile(market, None)
        stock_type, future_type = _load_ib_contract_types()
        resolved: Any

        if profile.sec_type == "STK":
            candidate = stock_type(symbol=code, exchange=profile.exchange, currency=profile.currency)
            qualified = ib.qualifyContracts(candidate)
            if not qualified:
                raise RuntimeError(f"failed to qualify stock contract: market={market}, code={code}")
            resolved = qualified[0]
        elif profile.sec_type == "FUT":
            if contract_month:
                candidate = future_type(
                    symbol=code,
                    lastTradeDateOrContractMonth=contract_month,
                    exchange=profile.exchange,
                    currency=profile.currency,
                )
                qualified = ib.qualifyContracts(candidate)
                if not qualified:
                    raise RuntimeError(
                        f"failed to qualify future contract: market={market}, code={code}, month={contract_month}"
                    )
                resolved = qualified[0]
            else:
                probe = future_type(symbol=code, exchange=profile.exchange, currency=profile.currency)
                details = ib.reqContractDetails(probe)
                if details:
                    resolved = self._pick_front_contract(ib, details)
                else:
                    fallback = future_type(
                        localSymbol=code,
                        exchange=profile.exchange,
                        currency=profile.currency,
                    )
                    qualified = ib.qualifyContracts(fallback)
                    if not qualified:
                        raise RuntimeError(
                            f"failed to resolve future contract: market={market}, code={code}; "
                            "try contract_month for explicit contract"
                        )
                    resolved = qualified[0]
        else:
            raise RuntimeError(f"unsupported sec_type for market={market}: {profile.sec_type}")

        with self._cache_lock:
            self._contract_cache[cache_key] = resolved
        return resolved

    def _pick_front_contract(self, ib: Any, details: list[Any]) -> Any:
        today = datetime.now(UTC).strftime("%Y%m%d")
        entries: list[tuple[str, str, Any]] = []
        for detail in details:
            contract = detail.contract
            month = str(getattr(contract, "lastTradeDateOrContractMonth", "")).strip()
            if month:
                entries.append((month, self._to_cmp_day(month), contract))
        if not entries:
            return details[0].contract

        ordered = sorted(entries, key=lambda item: item[1])
        for _, cmp_day, contract in ordered:
            if cmp_day >= today:
                qualified = ib.qualifyContracts(contract)
                if qualified:
                    return qualified[0]
        qualified = ib.qualifyContracts(ordered[-1][2])
        if qualified:
            return qualified[0]
        raise RuntimeError("failed to qualify resolved future contract")

    def _to_cmp_day(self, value: str) -> str:
        raw = value.strip()
        if len(raw) >= 8 and raw[:8].isdigit():
            return raw[:8]
        if len(raw) == 6 and raw.isdigit():
            return raw + "99"
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        if len(digits) == 6:
            return digits + "99"
        return "00000000"
