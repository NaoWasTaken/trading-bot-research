from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

OANDA_CANDLE_MAX_COUNT = 5000


class OandaApiError(RuntimeError):
    """Raised when the OANDA API returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class InstrumentMetadata:
    name: str
    display_precision: int
    pip_location: int
    minimum_trade_size: Decimal
    trade_units_precision: int


@dataclass(frozen=True)
class PositionSnapshot:
    instrument: str
    long_units: Decimal
    short_units: Decimal
    net_units: Decimal


@dataclass(frozen=True)
class PricingSnapshot:
    instrument: str
    time: str
    status: str
    bid: float
    ask: float


class OandaClient:
    def __init__(self, api_key: str, account_id: str, base_url: str, timeout: float = 15.0) -> None:
        self.account_id = account_id
        self._last_response: httpx.Response | None = None
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            },
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "OandaClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.client.request(method, path, params=params, json=json)
        except httpx.TooManyRedirects as exc:
            request_url = exc.request.url if exc.request is not None else "n/a"
            raise OandaApiError(
                f"OANDA API redirected too many times for {method} {request_url}.",
            ) from exc
        self._last_response = response

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.is_redirect:
            raise OandaApiError(
                "Unexpected OANDA redirect response. "
                f"{_format_response_debug(response)}",
                status_code=response.status_code,
                payload=payload,
            )

        if response.is_error:
            error_message = payload.get("errorMessage") or payload.get("errorCode") or response.text
            raise OandaApiError(
                f"OANDA API error {response.status_code}: {error_message}. {_format_response_debug(response)}",
                status_code=response.status_code,
                payload=payload,
            )

        return payload

    def get_account_summary(self) -> dict[str, Any]:
        return self._request("GET", f"/v3/accounts/{self.account_id}/summary")

    def get_instrument_details(self, instrument: str) -> InstrumentMetadata:
        payload = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/instruments",
            params={"instruments": instrument},
        )
        instruments = payload.get("instruments", [])
        if not isinstance(instruments, list):
            response_debug = _format_response_debug(self._last_response)
            raise OandaApiError(
                f"OANDA instrument metadata response did not include an instruments list. {response_debug}"
            )

        item = next(
            (
                candidate
                for candidate in instruments
                if isinstance(candidate, dict) and candidate.get("name") == instrument
            ),
            None,
        )
        if item is None:
            response_debug = _format_response_debug(self._last_response)
            returned = ", ".join(str(candidate.get("name")) for candidate in instruments if isinstance(candidate, dict))
            raise OandaApiError(
                f"Instrument {instrument} was not returned for this account. "
                f"Returned instruments: {returned or 'none'}. {response_debug}"
            )

        return InstrumentMetadata(
            name=item["name"],
            display_precision=int(item["displayPrecision"]),
            pip_location=int(item["pipLocation"]),
            minimum_trade_size=Decimal(item["minimumTradeSize"]),
            trade_units_precision=int(item["tradeUnitsPrecision"]),
        )

    def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int,
        price: str = "M",
        from_time: str | None = None,
        to_time: str | None = None,
        include_first: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "price": price,
            "granularity": granularity,
        }

        if from_time is not None:
            params["from"] = from_time
        if to_time is not None:
            params["to"] = to_time
        if include_first is not None:
            params["includeFirst"] = str(include_first).lower()
        if not (from_time is not None and to_time is not None):
            params["count"] = count

        payload = self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params=params,
        )

        return _parse_midpoint_candles(payload)

    def get_historical_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        count: int | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        price: str = "M",
    ) -> list[dict[str, Any]]:
        if count is None and from_time is None and to_time is None:
            raise ValueError("Provide either count or a date range for historical candles.")

        if count is not None and count <= 0:
            raise ValueError("Historical candle count must be greater than zero.")

        if from_time is not None or to_time is not None:
            start = _parse_oanda_datetime(from_time, is_end=False) if from_time is not None else None
            end = _parse_oanda_datetime(to_time, is_end=True) if to_time is not None else None

            if start is None and end is None:
                raise ValueError("Invalid historical candle date range.")
            if end is None:
                end = datetime.now(timezone.utc)
            if start is None:
                if count is None:
                    raise ValueError("BACKTEST_START_DATE is required when only BACKTEST_END_DATE is provided.")
                seconds = granularity_to_seconds(granularity)
                start = end - timedelta(seconds=seconds * count)
            if start >= end:
                raise ValueError("BACKTEST_START_DATE must be before BACKTEST_END_DATE.")

            return self._get_candles_by_time_range(
                instrument,
                granularity,
                start=start,
                end=end,
                price=price,
            )

        assert count is not None
        if count <= OANDA_CANDLE_MAX_COUNT:
            return self.get_candles(instrument, granularity, count, price=price)

        return self._get_latest_candles_paginated(
            instrument,
            granularity,
            count=count,
            price=price,
        )

    def _get_latest_candles_paginated(
        self,
        instrument: str,
        granularity: str,
        *,
        count: int,
        price: str,
    ) -> list[dict[str, Any]]:
        seconds = granularity_to_seconds(granularity)
        page_seconds = seconds * OANDA_CANDLE_MAX_COUNT
        candles_by_time: dict[str, dict[str, Any]] = {}

        first_page = self.get_candles(
            instrument,
            granularity,
            OANDA_CANDLE_MAX_COUNT,
            price=price,
        )
        for candle in first_page:
            candles_by_time[str(candle["time"])] = candle

        if not first_page:
            return []

        page_to = _parse_oanda_datetime(str(first_page[0]["time"]), is_end=False)

        while len(candles_by_time) < count:
            page_from = page_to - timedelta(seconds=page_seconds)
            page = self.get_candles(
                instrument,
                granularity,
                OANDA_CANDLE_MAX_COUNT,
                price=price,
                from_time=_format_oanda_datetime(page_from),
                to_time=_format_oanda_datetime(page_to),
                include_first=True,
            )

            for candle in page:
                candles_by_time[str(candle["time"])] = candle

            if page:
                earliest_time = _parse_oanda_datetime(str(page[0]["time"]), is_end=False)
                page_to = min(page_from, earliest_time)
            else:
                page_to = page_from

            if page_to.year < 1970:
                break

        candles = sorted(candles_by_time.values(), key=lambda candle: str(candle["time"]))
        return candles[-count:]

    def _get_candles_by_time_range(
        self,
        instrument: str,
        granularity: str,
        *,
        start: datetime,
        end: datetime,
        price: str,
    ) -> list[dict[str, Any]]:
        seconds = granularity_to_seconds(granularity)
        page_seconds = seconds * OANDA_CANDLE_MAX_COUNT
        page_from = start
        candles_by_time: dict[str, dict[str, Any]] = {}

        while page_from < end:
            page_to = min(page_from + timedelta(seconds=page_seconds), end)
            page = self.get_candles(
                instrument,
                granularity,
                OANDA_CANDLE_MAX_COUNT,
                price=price,
                from_time=_format_oanda_datetime(page_from),
                to_time=_format_oanda_datetime(page_to),
                include_first=True,
            )

            for candle in page:
                candles_by_time[str(candle["time"])] = candle

            if page_to >= end:
                break
            page_from = page_to

        return sorted(candles_by_time.values(), key=lambda candle: str(candle["time"]))

    def get_open_position(self, instrument: str) -> PositionSnapshot | None:
        payload = self._request("GET", f"/v3/accounts/{self.account_id}/openPositions")

        for item in payload.get("positions", []):
            if item.get("instrument") != instrument:
                continue

            long_units = Decimal(item["long"]["units"])
            short_units = Decimal(item["short"]["units"])
            return PositionSnapshot(
                instrument=instrument,
                long_units=long_units,
                short_units=short_units,
                net_units=long_units + short_units,
            )

        return None

    def get_current_pricing(self, instrument: str) -> PricingSnapshot:
        payload = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params={
                "instruments": instrument,
                "includeUnitsAvailable": False,
            },
        )

        for item in payload.get("prices", []):
            if item.get("instrument") != instrument:
                continue

            bids = item.get("bids", [])
            asks = item.get("asks", [])

            if bids:
                bid = float(bids[0]["price"])
            elif item.get("closeoutBid") is not None:
                bid = float(item["closeoutBid"])
            else:
                raise OandaApiError(f"OANDA pricing response did not include a bid for {instrument}.")

            if asks:
                ask = float(asks[0]["price"])
            elif item.get("closeoutAsk") is not None:
                ask = float(item["closeoutAsk"])
            else:
                raise OandaApiError(f"OANDA pricing response did not include an ask for {instrument}.")

            return PricingSnapshot(
                instrument=instrument,
                time=item["time"],
                status=item.get("status", "unknown"),
                bid=bid,
                ask=ask,
            )

        raise OandaApiError(f"Pricing for instrument {instrument} was not returned for this account.")

    def create_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: str,
    ) -> dict[str, Any]:
        if not stop_loss_price:
            raise ValueError("stop_loss_price is required for every order.")

        body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {
                    "timeInForce": "GTC",
                    "price": stop_loss_price,
                },
            }
        }

        return self._request("POST", f"/v3/accounts/{self.account_id}/orders", json=body)


def _parse_midpoint_candles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for item in payload.get("candles", []):
        if not item.get("complete"):
            continue

        midpoint = item.get("mid")
        if midpoint is None:
            continue

        candles.append(
            {
                "time": item["time"],
                "open": float(midpoint["o"]),
                "high": float(midpoint["h"]),
                "low": float(midpoint["l"]),
                "close": float(midpoint["c"]),
                "volume": int(item.get("volume", 0)),
            }
        )

    return candles


def _format_response_debug(response: httpx.Response | None) -> str:
    if response is None:
        return "response status code=n/a; final URL=n/a; Location header=n/a; response text=n/a"

    location = response.headers.get("Location", "n/a")
    text = response.text.strip() or "n/a"
    return (
        f"response status code={response.status_code}; "
        f"final URL={response.url}; "
        f"Location header={location}; "
        f"response text={text}"
    )


def _parse_oanda_datetime(value: str | None, *, is_end: bool) -> datetime:
    if value is None:
        raise ValueError("Datetime value cannot be None.")

    value = value.strip()
    if not value:
        raise ValueError("Datetime value cannot be empty.")

    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        parsed_date = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        if is_end:
            return parsed_date + timedelta(days=1)
        return parsed_date

    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        date_part, rest = normalized.split(".", 1)
        if "+" in rest:
            fraction, zone = rest.split("+", 1)
            normalized = f"{date_part}.{fraction[:6]}+{zone}"
        elif "-" in rest:
            fraction, zone = rest.split("-", 1)
            normalized = f"{date_part}.{fraction[:6]}-{zone}"
        else:
            normalized = f"{date_part}.{rest[:6]}"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_oanda_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def granularity_to_seconds(granularity: str) -> int:
    values = {
        "S5": 5,
        "S10": 10,
        "S15": 15,
        "S30": 30,
        "M1": 60,
        "M2": 120,
        "M3": 180,
        "M4": 240,
        "M5": 300,
        "M10": 600,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H2": 7200,
        "H3": 10800,
        "H4": 14400,
        "H6": 21600,
        "H8": 28800,
        "H12": 43200,
        "D": 86400,
        "W": 604800,
        "M": 2592000,
    }

    try:
        return values[granularity.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported candle granularity for pagination: {granularity}") from exc
