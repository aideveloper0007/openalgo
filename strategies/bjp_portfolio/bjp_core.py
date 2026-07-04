#!/usr/bin/env python
"""Shared risk-engine library for the BJP portfolio strategies.

This module is the single shared library imported by the 10 thin per-strategy
scripts in ``strategies/bjp_portfolio/``. It is **not** itself one of the 10
portfolio strategies: it implements none of them on its own and is never
scheduled or run directly (Req 1.2). It is a supporting library, analogous to
importing ``openalgo`` or ``apscheduler``.

Task 1.1 scope: this file defines the enums, configuration dataclasses, runtime
state dataclasses, the ``NIFTY``/``SENSEX`` index registry constants, and the
``ConfigError``/``ExpiryError`` exception types. Behavioural helpers
(environment resolution, strike math, expiry resolution, entry, monitoring,
risk evaluation, re-entry, exit, and the ``StrategyEngine``/``run`` orchestration)
are added by later tasks.

Targets Python 3.12+, 4-space indentation, Google-style docstrings.
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from time import sleep as _sleep
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("bjp_portfolio")

#: Default REST host used when neither HOST_SERVER nor OPENALGO_HOST is set.
DEFAULT_HOST = "http://127.0.0.1:5000"

#: Default monitoring interval (seconds) applied when the configured value is
#: non-numeric or outside the accepted 0.1-60s range (Req 14.3).
DEFAULT_MONITORING_INTERVAL = 1.0

#: Inclusive bounds for the monitoring interval, in seconds (Req 14.2).
MIN_MONITORING_INTERVAL = 0.1
MAX_MONITORING_INTERVAL = 60.0

#: Inclusive bounds for the configurable number of lots (Req 3.1).
MIN_LOTS = 1
MAX_LOTS = 100

#: Maximum number of attempts for each bounded-retry SDK operation: entry-leg
#: placement (Req 6.5), average-fill retrieval (Req 7.6), and square-off
#: (Req 10.6). This caps *total* attempts, not retries after the first try.
MAX_RETRY_ATTEMPTS = 3

#: Interval (seconds) between average-fill retrieval attempts (Req 7.6).
AVG_FILL_RETRY_INTERVAL = 2.0

#: Mapping of ``StrategyConfig`` field name -> environment-variable name used to
#: override that field before validation (Req 3.7). These are the discoverable,
#: documented override names that later config tests (3.2-3.5) rely on.
ENV_OVERRIDES: dict[str, str] = {
    "lots": "BJP_LOTS",
    "index": "BJP_INDEX",
    "entry_time": "BJP_ENTRY_TIME",
    "exit_time": "BJP_EXIT_TIME",
    "execution_mode": "BJP_EXECUTION_MODE",
    "monitoring_interval": "BJP_MONITORING_INTERVAL",
    "product": "BJP_PRODUCT",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration or environment resolution fails.

    Callers treat this as fatal and exit non-zero without placing any orders
    (Req 2.5, 3.8, 3.9).
    """


class ExpiryError(Exception):
    """Raised when weekly-expiry resolution fails.

    Signalled on a non-success SDK status, zero returned expiries, the 10s
    timeout elapsing, or all expiries being in the past (Req 5.4, 5.5). No
    entry orders are placed when this is raised.
    """


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OptionType(str, Enum):
    """Option right for a leg."""

    CE = "CE"
    PE = "PE"


class Action(str, Enum):
    """Order side for a leg."""

    BUY = "BUY"
    SELL = "SELL"


class SLKind(str, Enum):
    """Leg stop-loss flavor.

    ``POINTS``: premium >= entry_fill + P.
    ``PERCENTAGE``: premium >= entry_fill * (1 + X / 100).
    ``UNDERLYING_POINTS``: short CE spot >= entry_spot + U; short PE spot <=
    entry_spot - U.
    """

    POINTS = "Points"
    PERCENTAGE = "Percentage"
    UNDERLYING_POINTS = "UnderlyingPoints"


class ReentryKind(str, Enum):
    """Re-entry behavior after a leg stop-loss close.

    ``IMMEDIATE``: re-enter at prevailing market price.
    ``AT_COST``: re-enter at the original entry fill price when premium returns
    to it.
    """

    IMMEDIATE = "Immediate"
    AT_COST = "AtCost"


class ExecutionMode(str, Enum):
    """Order-routing mode. Defaults to sandbox when unconfigured (Req 16.1)."""

    SANDBOX = "sandbox"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LegStopLoss:
    """Static leg stop loss.

    Attributes:
        kind: The stop-loss flavor.
        value: P points, X percent, or U underlying points depending on ``kind``.
    """

    kind: SLKind
    value: float


@dataclass
class LegTrailSL:
    """Leg trailing stop-loss ratchet parameters.

    Attributes:
        instrument_move: I, the favorable premium fall per ratchet step (> 0).
        stoploss_move: S, how far the trail level tightens per step (0 < S <= I).
    """

    instrument_move: float
    stoploss_move: float


@dataclass
class LegMomentum:
    """Momentum-gated entry parameters.

    Attributes:
        points_down: N, the number of points the premium must fall from the
            reference before the leg is entered.
    """

    points_down: float


@dataclass
class LegReentry:
    """Leg re-entry configuration.

    Attributes:
        kind: Immediate or AtCost.
        count: The maximum number of re-entries; one of 1, 3, or 5.
    """

    kind: ReentryKind
    count: int


@dataclass
class LegConfig:
    """Static configuration for a single option leg."""

    option_type: OptionType
    action: Action
    offset: str  # "ATM" | "OTMn" | "ITMn"
    stop_loss: LegStopLoss | None = None
    trail_sl: LegTrailSL | None = None
    momentum: LegMomentum | None = None
    reentry: LegReentry | None = None


@dataclass
class OverallStopLoss:
    """Portfolio-level MTM stop loss.

    Attributes:
        mtm_rupees: L, the aggregate MTM loss (in rupees) that triggers
            square-off.
    """

    mtm_rupees: float


@dataclass
class OverallTrailSL:
    """Portfolio-level trailing stop-loss ratchet parameters.

    Attributes:
        instrument_move: I, the MTM improvement per ratchet step.
        stoploss_move: S, how far the locked level rises per step.
    """

    instrument_move: float
    stoploss_move: float


@dataclass
class IndexSpec:
    """Static description of a tradable index.

    Attributes:
        name: "NIFTY" or "SENSEX".
        index_exchange: Spot/index exchange, e.g. "NSE_INDEX" / "BSE_INDEX".
        fno_exchange: Options exchange, e.g. "NFO" / "BFO".
        lot_size: Contracts per lot (65 for NIFTY, 20 for SENSEX).
        strike_step: Strike spacing (50 for NIFTY, 100 for SENSEX).
    """

    name: str
    index_exchange: str
    fno_exchange: str
    lot_size: int
    strike_step: int


@dataclass
class StrategyConfig:
    """Full configuration for a single portfolio strategy."""

    strategy_name: str
    index: IndexSpec
    lots: int  # 1..100
    entry_time: str  # "HH:MM:SS" IST
    exit_time: str  # "HH:MM:SS" IST
    legs: list[LegConfig] = field(default_factory=list)
    overall_stop_loss: OverallStopLoss | None = None
    overall_trail_sl: OverallTrailSL | None = None
    square_off_all_legs: bool = False
    reentry_time_restriction_min: int | None = None  # e.g., 284
    product: str = "NRML"
    execution_mode: ExecutionMode = ExecutionMode.SANDBOX
    monitoring_interval: float = 1.0
    use_websocket: bool = False

    @property
    def quantity(self) -> int:
        """Total order quantity: lots multiplied by the index lot size (Req 3.2)."""
        return self.lots * self.index.lot_size


# ---------------------------------------------------------------------------
# Runtime state dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LegState:
    """Mutable runtime state for a single leg."""

    config: LegConfig
    symbol: str | None = None
    order_id: str | None = None
    entry_fill: float | None = None  # average_price from orderstatus
    entry_spot: float | None = None  # spot at entry (UnderlyingPoints baseline)
    last_ltp: float | None = None
    is_open: bool = False
    trail_level: float | None = None  # current trailing stop (monotone down)
    trail_ref: float | None = None  # last level at which trail advanced
    reentries_done: int = 0
    momentum_ref: float | None = None  # reference premium for PointsDown
    pending_momentum: bool = False  # awaiting momentum trigger
    exit_reason: str | None = None


@dataclass
class EngineState:
    """Mutable runtime state for the whole strategy engine."""

    config: StrategyConfig
    client: object
    expiry: str | None = None
    legs: list[LegState] = field(default_factory=list)
    entered_today: bool = False  # MaxPositionInADay = 1 (Req 6.3)
    peak_mtm: float = 0.0  # for overall trail
    locked_mtm_stop: float | None = None  # overall trail locked level
    realized_mtm: float = 0.0
    last_spot: float | None = None  # last-known underlying spot (Req 14.6)
    running: bool = True


# ---------------------------------------------------------------------------
# Index registry
# ---------------------------------------------------------------------------

NIFTY = IndexSpec(
    name="NIFTY",
    index_exchange="NSE_INDEX",
    fno_exchange="NFO",
    lot_size=65,
    strike_step=50,
)

SENSEX = IndexSpec(
    name="SENSEX",
    index_exchange="BSE_INDEX",
    fno_exchange="BFO",
    lot_size=20,
    strike_step=100,
)

#: Supported index registry keyed by canonical name.
INDEX_REGISTRY: dict[str, IndexSpec] = {
    NIFTY.name: NIFTY,
    SENSEX.name: SENSEX,
}


# ---------------------------------------------------------------------------
# Environment resolver (Req 2)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedEnv:
    """Platform-injected connection configuration resolved from the environment.

    Attributes:
        api_key: The resolved OpenAlgo API key (never empty; a missing key
            raises ``ConfigError`` instead of producing this object).
        host: The resolved REST host endpoint.
        ws_url: The resolved WebSocket endpoint, or ``None`` when no endpoint
            could be resolved. A ``None`` value signals that monitoring must
            fall back to polling (Req 2.4).
    """

    api_key: str
    host: str
    ws_url: str | None = None


def _env_nonempty(name: str) -> str | None:
    """Return the environment variable value if set and non-empty, else None.

    A value consisting only of whitespace is treated as empty.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_environment(use_websocket: bool = False) -> ResolvedEnv:
    """Resolve API key, REST host, and WebSocket endpoint from the environment.

    Precedence rules (Req 2):
        * ``api_key`` = ``OPENALGO_API_KEY``; unset/empty raises ``ConfigError``
          so the caller can log and exit non-zero (Req 2.1, 2.5).
        * ``host`` = first non-empty of ``HOST_SERVER``, ``OPENALGO_HOST``,
          else ``DEFAULT_HOST`` (Req 2.2).
        * ``ws_url`` = ``WEBSOCKET_URL`` if non-empty, else constructed as
          ``ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}`` when both parts are present
          (Req 2.3). When WebSocket monitoring is enabled and the parts are
          incomplete, the missing variable is logged and ``ws_url`` is ``None``
          to signal the polling fallback without exiting (Req 2.4).

    Args:
        use_websocket: Whether WebSocket monitoring is enabled. Controls whether
            an incomplete WebSocket configuration is reported as an error and
            triggers the documented polling fallback.

    Returns:
        A ``ResolvedEnv`` with the resolved api_key, host, and ws_url.

    Raises:
        ConfigError: If ``OPENALGO_API_KEY`` is unset or empty (Req 2.5).
    """
    api_key = _env_nonempty("OPENALGO_API_KEY")
    if api_key is None:
        logger.error("OPENALGO_API_KEY is unset or empty; cannot start strategy.")
        raise ConfigError("OPENALGO_API_KEY is unset or empty")

    host = _env_nonempty("HOST_SERVER") or _env_nonempty("OPENALGO_HOST") or DEFAULT_HOST

    ws_url = _resolve_ws_url(use_websocket)

    return ResolvedEnv(api_key=api_key, host=host, ws_url=ws_url)


def _resolve_ws_url(use_websocket: bool) -> str | None:
    """Resolve the WebSocket endpoint per Req 2.3 and 2.4.

    Returns the explicit ``WEBSOCKET_URL`` when set, otherwise a URL built from
    ``WEBSOCKET_HOST`` and ``WEBSOCKET_PORT`` when both are present. When the
    parts are incomplete and ``use_websocket`` is True, the missing variable is
    logged and ``None`` is returned to signal the polling fallback (Req 2.4).
    """
    ws_url = _env_nonempty("WEBSOCKET_URL")
    if ws_url is not None:
        return ws_url

    ws_host = _env_nonempty("WEBSOCKET_HOST")
    ws_port = _env_nonempty("WEBSOCKET_PORT")
    if ws_host is not None and ws_port is not None:
        return f"ws://{ws_host}:{ws_port}"

    if use_websocket:
        missing = []
        if ws_host is None:
            missing.append("WEBSOCKET_HOST")
        if ws_port is None:
            missing.append("WEBSOCKET_PORT")
        logger.error(
            "WebSocket monitoring enabled but %s unset or empty (WEBSOCKET_URL "
            "also unset); falling back to polling.",
            " and ".join(missing),
        )

    return None


# ---------------------------------------------------------------------------
# Config validator (Req 3, 16)
# ---------------------------------------------------------------------------


def _apply_env_overrides(config: StrategyConfig) -> None:
    """Apply environment-variable overrides onto ``config`` in place (Req 3.7).

    For each overridable field, when the corresponding environment variable
    (see :data:`ENV_OVERRIDES`) is set and non-empty, its value replaces the
    in-script default *before* validation runs, so an operator can retune a
    strategy from the launch environment without editing the script.

    Coercion rules:
        * ``BJP_LOTS`` is parsed to ``int``; a non-integer value raises
          ``ConfigError`` naming ``lots``.
        * ``BJP_INDEX`` is looked up (case-insensitively) in
          :data:`INDEX_REGISTRY`; an unsupported name raises ``ConfigError``
          naming ``index``.
        * ``BJP_ENTRY_TIME`` / ``BJP_EXIT_TIME`` / ``BJP_PRODUCT`` are assigned
          as raw strings and validated downstream.
        * ``BJP_EXECUTION_MODE`` and ``BJP_MONITORING_INTERVAL`` are assigned
          raw and coerced/validated by :func:`validate_config` (a non-numeric
          interval coerces to the default rather than failing, per Req 14.3).

    Raises:
        ConfigError: If ``BJP_LOTS`` is non-integer or ``BJP_INDEX`` names an
            unsupported index.
    """
    lots_raw = _env_nonempty(ENV_OVERRIDES["lots"])
    if lots_raw is not None:
        try:
            config.lots = int(lots_raw)
        except ValueError as exc:
            logger.error(
                "Invalid config parameter 'lots' (%s=%r): not an integer.",
                ENV_OVERRIDES["lots"],
                lots_raw,
            )
            raise ConfigError(
                f"lots override {ENV_OVERRIDES['lots']}={lots_raw!r} is not an integer"
            ) from exc

    index_raw = _env_nonempty(ENV_OVERRIDES["index"])
    if index_raw is not None:
        index_spec = INDEX_REGISTRY.get(index_raw.upper())
        if index_spec is None:
            logger.error(
                "Invalid config parameter 'index' (%s=%r): unsupported index.",
                ENV_OVERRIDES["index"],
                index_raw,
            )
            raise ConfigError(
                f"index override {ENV_OVERRIDES['index']}={index_raw!r} is unsupported; "
                f"supported: {sorted(INDEX_REGISTRY)}"
            )
        config.index = index_spec

    entry_raw = _env_nonempty(ENV_OVERRIDES["entry_time"])
    if entry_raw is not None:
        config.entry_time = entry_raw

    exit_raw = _env_nonempty(ENV_OVERRIDES["exit_time"])
    if exit_raw is not None:
        config.exit_time = exit_raw

    mode_raw = _env_nonempty(ENV_OVERRIDES["execution_mode"])
    if mode_raw is not None:
        config.execution_mode = mode_raw  # coerced/validated below

    interval_raw = _env_nonempty(ENV_OVERRIDES["monitoring_interval"])
    if interval_raw is not None:
        config.monitoring_interval = interval_raw  # coerced below

    product_raw = _env_nonempty(ENV_OVERRIDES["product"])
    if product_raw is not None:
        config.product = product_raw


def _coerce_monitoring_interval(value: object) -> float:
    """Return a valid monitoring interval, coercing invalid input to the default.

    A value is accepted only when it is numeric (``int``/``float``, excluding
    ``bool``) or a numeric string, and lies within
    ``[MIN_MONITORING_INTERVAL, MAX_MONITORING_INTERVAL]``. Any non-numeric or
    out-of-range value is logged and coerced to
    :data:`DEFAULT_MONITORING_INTERVAL` rather than failing validation
    (Req 3.6, 14.2, 14.3).
    """
    numeric: float | None
    if isinstance(value, bool):
        numeric = None
    elif isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value.strip())
        except ValueError:
            numeric = None
    else:
        numeric = None

    if numeric is None or not (
        MIN_MONITORING_INTERVAL <= numeric <= MAX_MONITORING_INTERVAL
    ):
        logger.error(
            "Invalid config parameter 'monitoring_interval' (%r): must be numeric "
            "within %.1f-%.1fs; coercing to default %.1fs.",
            value,
            MIN_MONITORING_INTERVAL,
            MAX_MONITORING_INTERVAL,
            DEFAULT_MONITORING_INTERVAL,
        )
        return DEFAULT_MONITORING_INTERVAL
    return numeric


def _coerce_execution_mode(value: object) -> ExecutionMode:
    """Return a valid :class:`ExecutionMode`, defaulting/validating as needed.

    ``None`` defaults to sandbox (Req 16.1); an existing ``ExecutionMode`` is
    returned as-is; a string is matched case-insensitively against the
    supported values. Anything else raises ``ConfigError`` naming
    ``execution_mode`` (Req 16.5).
    """
    if value is None:
        return ExecutionMode.SANDBOX
    if isinstance(value, ExecutionMode):
        return value
    if isinstance(value, str):
        try:
            return ExecutionMode(value.strip().lower())
        except ValueError as exc:
            logger.error(
                "Invalid config parameter 'execution_mode' (%r): must be "
                "'sandbox' or 'live'.",
                value,
            )
            raise ConfigError(
                f"execution_mode {value!r} is invalid; must be 'sandbox' or 'live'"
            ) from exc
    logger.error(
        "Invalid config parameter 'execution_mode' (%r): must be 'sandbox' or 'live'.",
        value,
    )
    raise ConfigError(
        f"execution_mode {value!r} is invalid; must be 'sandbox' or 'live'"
    )


def _parse_hms(value: object, field_name: str) -> "datetime":
    """Parse an ``HH:MM:SS`` 24-hour time string, or raise ``ConfigError``.

    Returns a ``datetime`` (on an arbitrary fixed date) suitable for ordering
    comparison. Raises ``ConfigError`` naming ``field_name`` when the value is
    not a valid ``HH:MM:SS`` string (Req 3.4, 3.9).
    """
    if not isinstance(value, str):
        logger.error(
            "Invalid config parameter %r (%r): must be an 'HH:MM:SS' string.",
            field_name,
            value,
        )
        raise ConfigError(f"{field_name} {value!r} must be an 'HH:MM:SS' string")
    try:
        return datetime.strptime(value.strip(), "%H:%M:%S")
    except ValueError as exc:
        logger.error(
            "Invalid config parameter %r (%r): must be a valid 24-hour 'HH:MM:SS' time.",
            field_name,
            value,
        )
        raise ConfigError(
            f"{field_name} {value!r} must be a valid 24-hour 'HH:MM:SS' time"
        ) from exc


def validate_config(config: StrategyConfig) -> StrategyConfig:
    """Validate and normalize a :class:`StrategyConfig` before any trading.

    Environment-variable overrides (see :data:`ENV_OVERRIDES`) are applied first
    (Req 3.7), then each field is validated. The monitoring interval is coerced
    rather than rejected when invalid; every other invalid field aborts
    validation.

    Override environment variables (all optional, applied when set and
    non-empty):
        * ``BJP_LOTS`` -> ``lots`` (integer)
        * ``BJP_INDEX`` -> ``index`` (``NIFTY`` or ``SENSEX``, case-insensitive)
        * ``BJP_ENTRY_TIME`` -> ``entry_time`` (``HH:MM:SS``)
        * ``BJP_EXIT_TIME`` -> ``exit_time`` (``HH:MM:SS``)
        * ``BJP_EXECUTION_MODE`` -> ``execution_mode`` (``sandbox`` or ``live``)
        * ``BJP_MONITORING_INTERVAL`` -> ``monitoring_interval`` (seconds)
        * ``BJP_PRODUCT`` -> ``product``

    Validation rules:
        * ``lots``: integer within 1-100 inclusive (Req 3.1).
        * ``index``: a supported :class:`IndexSpec` in :data:`INDEX_REGISTRY`
          (Req 3.3).
        * ``entry_time`` / ``exit_time``: valid 24-hour ``HH:MM:SS`` strings
          with ``exit_time`` strictly later than ``entry_time`` (Req 3.4, 3.8).
        * ``execution_mode``: ``sandbox`` or ``live``, defaulting to sandbox
          when unset (Req 16.1, 16.5).
        * ``monitoring_interval``: numeric within 0.1-60s, else logged and
          coerced to 1.0s (Req 3.6, 14.2, 14.3).

    Return/failure contract:
        On success, returns the same ``config`` instance with normalized
        ``execution_mode`` (enum), coerced ``monitoring_interval``, and any
        applied env overrides. On any invalid field (other than the coerced
        interval) it logs the offending parameter and raises :class:`ConfigError`
        naming that parameter; because ``ConfigError`` is fatal to the caller,
        no scheduling or trading proceeds (Req 3.8, 3.9, 16.5).

    Args:
        config: The strategy configuration to validate. Mutated in place with
            overrides and coerced values.

    Returns:
        The validated and normalized ``config``.

    Raises:
        ConfigError: If any field other than ``monitoring_interval`` is invalid.
    """
    _apply_env_overrides(config)

    # lots: integer within 1..100 (bool is a subclass of int and is rejected).
    lots = config.lots
    if isinstance(lots, bool) or not isinstance(lots, int) or not (
        MIN_LOTS <= lots <= MAX_LOTS
    ):
        logger.error(
            "Invalid config parameter 'lots' (%r): must be an integer in %d-%d.",
            lots,
            MIN_LOTS,
            MAX_LOTS,
        )
        raise ConfigError(
            f"lots {lots!r} is invalid; must be an integer in {MIN_LOTS}-{MAX_LOTS}"
        )

    # index: a supported index/exchange pair.
    index = config.index
    if not isinstance(index, IndexSpec) or index.name not in INDEX_REGISTRY:
        logger.error(
            "Invalid config parameter 'index' (%r): must be a supported index.",
            index,
        )
        raise ConfigError(
            f"index {index!r} is unsupported; supported: {sorted(INDEX_REGISTRY)}"
        )

    # execution_mode: coerce/validate (defaults to sandbox when unset).
    config.execution_mode = _coerce_execution_mode(config.execution_mode)

    # entry_time / exit_time: HH:MM:SS and exit strictly later than entry.
    entry_dt = _parse_hms(config.entry_time, "entry_time")
    exit_dt = _parse_hms(config.exit_time, "exit_time")
    if exit_dt <= entry_dt:
        logger.error(
            "Invalid config: exit_time (%r) must be later than entry_time (%r).",
            config.exit_time,
            config.entry_time,
        )
        raise ConfigError(
            f"exit_time {config.exit_time!r} must be later than "
            f"entry_time {config.entry_time!r}"
        )

    # monitoring_interval: coerce invalid/out-of-range values to the default.
    config.monitoring_interval = _coerce_monitoring_interval(config.monitoring_interval)

    return config


# ---------------------------------------------------------------------------
# Strike helpers (Req 4)
# ---------------------------------------------------------------------------

#: Inclusive bound on the offset distance ``n`` supported by the strike-type
#: mapping (Req 4.1). The backtest never selects a strike more than 50 steps
#: from the at-the-money reference.
MAX_OFFSET_DISTANCE = 50

#: Matches an offset/strike-type distance token such as ``OTM6`` or ``ITM2``,
#: capturing the direction token and its integer distance.
_OFFSET_RE = re.compile(r"^(OTM|ITM)(\d+)$")


def _normalize_option_type(option_type: OptionType | str) -> OptionType:
    """Coerce ``option_type`` to an :class:`OptionType`.

    Accepts either an :class:`OptionType` enum member or a ``"CE"``/``"PE"``
    string (case-insensitive) so callers may pass whichever is convenient.

    Raises:
        ValueError: If the value is not a recognized option type.
    """
    if isinstance(option_type, OptionType):
        return option_type
    return OptionType(str(option_type).strip().upper())


def _parse_offset(offset: str) -> tuple[str, int]:
    """Parse an offset string into a ``(direction, distance)`` pair.

    ``"ATM"`` parses to ``("ATM", 0)``; ``"OTMn"``/``"ITMn"`` parse to
    ``("OTM", n)`` / ``("ITM", n)``. The optional ``StrikeType.`` prefix from
    the AlgoTest backtest tokens is stripped and matching is case-insensitive.

    Args:
        offset: An offset or strike-type token, e.g. ``"ATM"``, ``"OTM6"``,
            or ``"StrikeType.ITM2"``.

    Returns:
        A ``(direction, distance)`` tuple where ``direction`` is one of
        ``"ATM"``, ``"OTM"``, ``"ITM"`` and ``distance`` is a non-negative int.

    Raises:
        ValueError: If ``offset`` is not a recognized token or its distance is
            outside 0-50 inclusive (Req 4.1).
    """
    if not isinstance(offset, str):
        raise ValueError(f"offset {offset!r} must be a string")

    token = offset.strip()
    if "." in token:
        token = token.rsplit(".", 1)[1]
    token = token.upper()

    if token == "ATM":
        return ("ATM", 0)

    match = _OFFSET_RE.match(token)
    if match is None:
        raise ValueError(
            f"offset {offset!r} is invalid; expected 'ATM', 'OTM<n>', or 'ITM<n>'"
        )
    direction = match.group(1)
    distance = int(match.group(2))
    if not (0 <= distance <= MAX_OFFSET_DISTANCE):
        raise ValueError(
            f"offset {offset!r} distance {distance} out of range 0-{MAX_OFFSET_DISTANCE}"
        )
    return (direction, distance)


def round_atm(spot: float, step: int) -> int:
    """Round ``spot`` to the nearest strike multiple, ties rounding upward.

    Uses ``math.floor(spot / step + 0.5)`` to guarantee half-up rounding (a spot
    exactly halfway between two multiples rounds to the higher multiple),
    avoiding banker's rounding. ``step`` is 50 for NIFTY and 100 for SENSEX
    (Req 4.4).

    Args:
        spot: The underlying spot price.
        step: The strike spacing (50 for NIFTY, 100 for SENSEX); must be > 0.

    Returns:
        The nearest strike as an ``int`` multiple of ``step``.

    Raises:
        ValueError: If ``step`` is not a positive integer.
    """
    if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
        raise ValueError(f"step {step!r} must be a positive integer")
    return int(math.floor(spot / step + 0.5)) * step


def apply_offset(
    atm: int, step: int, offset: str, option_type: OptionType | str
) -> int:
    """Resolve an OpenAlgo offset to an absolute strike for the given option.

    Direction semantics (Req 4.2, 4.3), one strike ``step`` per offset unit:
        * CE ``OTM<n>`` -> ``atm + n*step``; CE ``ITM<n>`` -> ``atm - n*step``.
        * PE ``OTM<n>`` -> ``atm - n*step``; PE ``ITM<n>`` -> ``atm + n*step``.
        * ``ATM`` -> ``atm`` for both option types.

    Args:
        atm: The at-the-money reference strike (see :func:`round_atm`).
        step: The strike spacing (50 for NIFTY, 100 for SENSEX); must be > 0.
        offset: The offset token, e.g. ``"ATM"``, ``"OTM6"``, ``"ITM2"``.
        option_type: ``OptionType.CE``/``OptionType.PE`` or the string
            ``"CE"``/``"PE"`` (case-insensitive).

    Returns:
        The absolute strike as an ``int``.

    Raises:
        ValueError: If ``step`` is invalid, ``offset`` is unrecognized, or
            ``option_type`` is not CE/PE.
    """
    if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
        raise ValueError(f"step {step!r} must be a positive integer")

    opt = _normalize_option_type(option_type)
    direction, distance = _parse_offset(offset)

    if direction == "ATM":
        return int(atm)

    # OTM raises the strike for CE and lowers it for PE; ITM is the mirror.
    is_otm = direction == "OTM"
    move_up = is_otm if opt is OptionType.CE else not is_otm
    delta = distance * step
    return int(atm + delta) if move_up else int(atm - delta)


def map_strike_type_to_offset(strike_type: str) -> str:
    """Map an AlgoTest ``StrikeType`` token to its OpenAlgo offset string.

    Preserves both the direction token and the integer distance for all
    distances ``n`` in 0-50 inclusive (Req 4.1):
    ``StrikeType.OTM<n>`` -> ``OTM<n>``, ``StrikeType.ITM<n>`` -> ``ITM<n>``,
    and ``StrikeType.ATM`` -> ``ATM``. The optional ``StrikeType.`` prefix is
    stripped and the input is matched case-insensitively; the returned token is
    canonical upper-case.

    Args:
        strike_type: The backtest strike selector, e.g. ``"StrikeType.OTM20"``,
            ``"ITM1"``, or ``"ATM"``.

    Returns:
        The canonical OpenAlgo offset string, e.g. ``"OTM20"``, ``"ITM1"``,
        ``"ATM"``.

    Raises:
        ValueError: If ``strike_type`` is not a recognized token or its distance
            is outside 0-50 inclusive.
    """
    direction, distance = _parse_offset(strike_type)
    if direction == "ATM":
        return "ATM"
    return f"{direction}{distance}"


# ---------------------------------------------------------------------------
# Expiry resolver (Req 5)
# ---------------------------------------------------------------------------

#: IST timezone used to derive the default current trading date (Req 5.2).
_IST = ZoneInfo("Asia/Kolkata")

#: Seconds allowed for the ``client.expiry()`` SDK call before the resolution is
#: treated as a failure (Req 5.1, 5.4).
EXPIRY_TIMEOUT_SECONDS = 10.0

#: Multi-format parser format strings, mirroring ``examples/python/expiry_dates.py``.
#: Covers dash/no-dash separators and short/long month names (Req 5.2).
_EXPIRY_FORMATS = ("%d-%b-%y", "%d%b%y", "%d-%B-%y", "%d%B%y")

#: Underlyings for which weekly expiries can be resolved, mapped to the F&O
#: exchange the backtest expects (Req 5.1, 5.3).
_EXPIRY_UNDERLYINGS: dict[str, str] = {
    NIFTY.name: NIFTY.fno_exchange,
    SENSEX.name: SENSEX.fno_exchange,
}


def _parse_expiry_string(exp_str: str) -> date | None:
    """Parse an expiry string to a ``date`` across the supported formats.

    Tries each format in :data:`_EXPIRY_FORMATS` in order (case-insensitive,
    whitespace-trimmed), returning the first that parses. Returns ``None`` when
    the string matches none of the formats so callers can skip unparseable
    entries rather than crash.

    Args:
        exp_str: A raw expiry token such as ``"31-DEC-25"`` or ``"31DEC25"``.

    Returns:
        The parsed ``date``, or ``None`` if no format matched.
    """
    if not isinstance(exp_str, str):
        return None
    normalized = exp_str.upper().strip()
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            continue
    return None


def resolve_weekly_expiry(
    client: object,
    underlying: str,
    fno_exchange: str,
    today: date | None = None,
) -> str:
    """Resolve the current-week expiry (earliest date on/after today).

    Calls ``client.expiry(symbol=underlying, exchange=fno_exchange,
    instrumenttype="options")`` inside a worker thread and waits at most
    :data:`EXPIRY_TIMEOUT_SECONDS` for the result; exceeding that budget is
    treated as a resolution failure (Req 5.1, 5.4). Returned expiry strings are
    parsed with the multi-format parser, sorted chronologically, and the
    earliest whose date is on or after the current trading date is returned
    (Req 5.2).

    Only NIFTY and SENSEX are supported; any other ``underlying`` is reported as
    an unsupported index and no ``client.expiry()`` call is made (Req 5.3).

    Args:
        client: The OpenAlgo SDK client exposing ``expiry()``.
        underlying: The index symbol, e.g. ``"NIFTY"`` or ``"SENSEX"``
            (case-insensitive).
        fno_exchange: The F&O exchange passed to the SDK (``"NFO"`` for NIFTY,
            ``"BFO"`` for SENSEX).
        today: The current trading date to compare against. Defaults to the
            current date in IST; injectable so tests can pin it deterministically.

    Returns:
        The selected current-week expiry as the original SDK-provided string
        (e.g. ``"31-DEC-25"``), unmodified.

    Raises:
        ExpiryError: If the index is unsupported, the SDK returns a non-success
            status, returns zero expiries, does not complete within
            :data:`EXPIRY_TIMEOUT_SECONDS`, or every returned expiry is in the
            past (Req 5.3, 5.4, 5.5).
    """
    name = str(underlying).strip().upper()
    if name not in _EXPIRY_UNDERLYINGS:
        logger.error(
            "Unsupported index %r for expiry resolution; supported: %s. "
            "Skipping client.expiry() and placing no entry orders.",
            underlying,
            sorted(_EXPIRY_UNDERLYINGS),
        )
        raise ExpiryError(
            f"unsupported index {underlying!r}; supported: {sorted(_EXPIRY_UNDERLYINGS)}"
        )

    trading_date = today if today is not None else datetime.now(_IST).date()

    # Enforce the 10s budget by running the blocking SDK call in a worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.expiry,
            symbol=name,
            exchange=fno_exchange,
            instrumenttype="options",
        )
        try:
            response = future.result(timeout=EXPIRY_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            logger.error(
                "Expiry resolution for %s did not complete within %.0fs; "
                "placing no entry orders.",
                name,
                EXPIRY_TIMEOUT_SECONDS,
            )
            raise ExpiryError(
                f"expiry resolution for {name} timed out after "
                f"{EXPIRY_TIMEOUT_SECONDS:.0f}s"
            ) from exc

    if not isinstance(response, dict) or response.get("status") != "success":
        message = None
        if isinstance(response, dict):
            message = response.get("message")
        logger.error(
            "Expiry resolution for %s returned non-success status (%s); "
            "placing no entry orders.",
            name,
            message,
        )
        raise ExpiryError(
            f"expiry resolution for {name} failed with non-success status: {message}"
        )

    expiries = response.get("data") or []
    if not expiries:
        logger.error(
            "Expiry resolution for %s returned zero expiries; placing no entry orders.",
            name,
        )
        raise ExpiryError(f"expiry resolution for {name} returned zero expiries")

    # Parse, drop unparseable entries, sort chronologically by parsed date.
    parsed: list[tuple[date, str]] = []
    for exp_str in expiries:
        parsed_date = _parse_expiry_string(exp_str)
        if parsed_date is not None:
            parsed.append((parsed_date, exp_str))
    parsed.sort(key=lambda item: item[0])

    for parsed_date, exp_str in parsed:
        if parsed_date >= trading_date:
            return exp_str

    logger.error(
        "No current or future weekly expiry available for %s on/after %s; "
        "placing no entry orders.",
        name,
        trading_date.isoformat(),
    )
    raise ExpiryError(
        f"no current or future weekly expiry for {name} on/after "
        f"{trading_date.isoformat()}"
    )


# ---------------------------------------------------------------------------
# MTM computation (Req 13.4)
# ---------------------------------------------------------------------------


def leg_mtm(
    leg_state: LegState,
    quantity: int,
    ltp: float | None = None,
) -> float | None:
    """Compute a single leg's mark-to-market P&L in rupees.

    Sign convention (Req 13.4), where ``entry`` is the leg's average entry fill
    price and ``price`` its current LTP:
        * ``SELL`` (short) leg: ``(entry - price) * quantity`` — the position
          profits as the premium falls below the entry fill.
        * ``BUY`` (long) leg: ``(price - entry) * quantity`` — the position
          profits as the premium rises above the entry fill.

    This is a pure function: it reads only the supplied arguments and the leg's
    ``config.action``/``entry_fill``/``last_ltp`` fields and mutates nothing.

    Contract for missing data:
        The effective price is ``ltp`` when provided, otherwise
        ``leg_state.last_ltp``. When either the entry fill or the effective
        price is unavailable (``None``), the leg's MTM is undefined and this
        function returns ``None`` rather than guessing a value. Callers such as
        :func:`aggregate_mtm` skip such legs. (Requirement 13.6's stale-data
        cycle-skip is enforced by the monitoring loop, not here.)

    Args:
        leg_state: The leg whose MTM is computed. ``config.action`` selects the
            sign; ``entry_fill`` and ``last_ltp`` supply the prices.
        quantity: The total contract quantity for the leg (``lots * lot_size``).
        ltp: An explicit current LTP to use; defaults to ``leg_state.last_ltp``.

    Returns:
        The leg's MTM in rupees, or ``None`` when the entry fill or effective
        LTP is missing.

    Raises:
        ValueError: If the leg's action is neither BUY nor SELL.
    """
    entry = leg_state.entry_fill
    price = ltp if ltp is not None else leg_state.last_ltp
    if entry is None or price is None:
        return None

    action = Action(leg_state.config.action)
    if action is Action.SELL:
        return (entry - price) * quantity
    if action is Action.BUY:
        return (price - entry) * quantity
    raise ValueError(f"leg action {leg_state.config.action!r} is neither BUY nor SELL")


def aggregate_mtm(engine_state: EngineState) -> float:
    """Sum the mark-to-market P&L across all open legs (Req 13.4).

    Iterates the engine's legs, and for every leg that ``is_open`` computes its
    per-leg MTM via :func:`leg_mtm` using the strategy quantity
    (``engine_state.config.quantity``) and the leg's stored ``last_ltp``. Legs
    whose MTM is undefined — because they lack an entry fill or a known LTP —
    contribute nothing to the sum, so this function always returns a concrete
    number.

    This is a pure function over the engine state: it reads leg prices/actions
    and the configured quantity and mutates nothing.

    Note:
        Requirement 13.6 (skip the whole aggregate evaluation and surface a
        stale-data error when *any* open leg lacks a fresh LTP) is enforced by
        the monitoring loop before it decides whether to call this function.
        This pure summation deliberately tolerates missing legs so it stays
        simple and independently testable.

    Args:
        engine_state: The engine state carrying the legs and the strategy
            configuration (for ``quantity``).

    Returns:
        The aggregate MTM in rupees summed over open legs that have both an
        entry fill and a known LTP; ``0.0`` when no such leg exists.
    """
    quantity = engine_state.config.quantity
    total = 0.0
    for leg in engine_state.legs:
        if not leg.is_open:
            continue
        value = leg_mtm(leg, quantity)
        if value is not None:
            total += value
    return total


# ---------------------------------------------------------------------------
# Leg stop-loss evaluators (Req 10)
# ---------------------------------------------------------------------------


def leg_stop_loss_premium_threshold(leg_state: LegState) -> float | None:
    """Return the premium level at which a premium-based leg SL triggers.

    Applies to the two premium flavors on a short (SELL) leg (Req 10.1, 10.2):
        * ``Points`` (P): ``entry_fill + P``.
        * ``Percentage`` (X): ``entry_fill * (1 + X / 100)``.

    The ``UnderlyingPoints`` flavor triggers on the underlying spot rather than
    the leg premium and therefore has no premium threshold; ``None`` is returned
    for it, as it is when the leg has no stop loss or no recorded entry fill.

    Exposing this level lets the leg-trailing-stop min-selection later enforce
    whichever of the base SL and the trail level is the lower, more protective
    premium (Req 11.5). This is a pure function that mutates nothing.

    Args:
        leg_state: The leg whose base premium SL level is computed. Reads
            ``config.stop_loss`` and ``entry_fill``.

    Returns:
        The adverse premium level as a ``float``, or ``None`` when the leg has
        no stop loss, no recorded entry fill, or an ``UnderlyingPoints`` SL.
    """
    sl = leg_state.config.stop_loss
    entry = leg_state.entry_fill
    if sl is None or entry is None:
        return None

    kind = SLKind(sl.kind)
    if kind is SLKind.POINTS:
        return entry + sl.value
    if kind is SLKind.PERCENTAGE:
        return entry * (1.0 + sl.value / 100.0)
    return None


def leg_stop_loss_triggered(
    leg_state: LegState,
    premium: float | None = None,
    spot: float | None = None,
) -> bool:
    """Return whether an open short leg's stop loss is triggered this cycle.

    Pure predicate evaluated on each monitoring cycle (Req 10.7) for a short
    (SELL) leg. The three flavors, all with **inclusive** thresholds, are
    (Req 10.1-10.4):
        * ``Points`` (P): triggers when ``premium >= entry_fill + P``.
        * ``Percentage`` (X): triggers when
          ``premium >= entry_fill * (1 + X / 100)``.
        * ``UnderlyingPoints`` (U): for a short CE triggers when
          ``spot >= entry_spot + U``; for a short PE triggers when
          ``spot <= entry_spot - U``.

    The premium flavors default ``premium`` to the leg's stored ``last_ltp``
    when not supplied; the underlying flavor requires ``spot`` to be supplied
    each cycle (there is no stored spot on the leg). When the inputs needed for
    the configured flavor are unavailable — no stop loss, no ``entry_fill`` for
    the premium flavors, no ``entry_spot``/``spot`` for the underlying flavor —
    the predicate returns ``False`` (no trigger) rather than guessing, matching
    the fail-safe "do not exit on missing data" posture. This is a pure function
    that mutates nothing.

    Args:
        leg_state: The open short leg to evaluate. Reads ``config.stop_loss``,
            ``config.option_type``, ``entry_fill``, ``entry_spot``, and
            ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp``. Used only by the ``Points``/``Percentage``
            flavors.
        spot: The current underlying spot; used only by the
            ``UnderlyingPoints`` flavor.

    Returns:
        ``True`` when the configured stop-loss threshold is met (inclusive),
        otherwise ``False``.
    """
    sl = leg_state.config.stop_loss
    if sl is None:
        return False

    kind = SLKind(sl.kind)

    if kind in (SLKind.POINTS, SLKind.PERCENTAGE):
        px = premium if premium is not None else leg_state.last_ltp
        threshold = leg_stop_loss_premium_threshold(leg_state)
        if px is None or threshold is None:
            return False
        return px >= threshold

    # UnderlyingPoints: compare the underlying spot against the entry-spot
    # baseline, with the adverse direction depending on the option right.
    entry_spot = leg_state.entry_spot
    if entry_spot is None or spot is None:
        return False

    option_type = _normalize_option_type(leg_state.config.option_type)
    if option_type is OptionType.CE:
        return spot >= entry_spot + sl.value
    return spot <= entry_spot - sl.value


# ---------------------------------------------------------------------------
# Leg trailing stop-loss ratchet (Req 11)
# ---------------------------------------------------------------------------


def init_leg_trail(leg_state: LegState) -> LegState:
    """Initialize a short leg's trailing stop level and ratchet reference.

    Sets up the two pieces of ratchet state on ``leg_state`` (Req 11.1):
        * ``trail_level`` — the initial trailing stop premium. This is the leg's
          base premium stop-loss level (via
          :func:`leg_stop_loss_premium_threshold`) when one exists, otherwise
          the leg's ``entry_fill`` price when no premium base SL is defined
          (this fallback also covers an ``UnderlyingPoints`` base SL, which has
          no premium level).
        * ``trail_ref`` — the reference premium from which favorable (downward)
          falls are measured. It starts at the leg's ``entry_fill`` and is
          advanced downward by :func:`update_leg_trail` on each complete
          ``I``-point fall.

    Mutation semantics:
        This function **mutates** ``leg_state.trail_level`` and
        ``leg_state.trail_ref`` in place; ratchet progress is inherently
        stateful. When the leg has no ``trail_sl`` configured, or has no
        recorded ``entry_fill`` yet, the state is left untouched (both fields
        stay ``None``) so callers can safely no-op until a fill is known. The
        (mutated) ``leg_state`` is returned for convenience/chaining.

    Args:
        leg_state: The short leg to initialize. Reads ``config.trail_sl``,
            ``config.stop_loss``, and ``entry_fill``.

    Returns:
        The same ``leg_state`` instance, with ``trail_level`` and ``trail_ref``
        set when initialization was possible.
    """
    if leg_state.config.trail_sl is None:
        return leg_state
    entry = leg_state.entry_fill
    if entry is None:
        return leg_state

    base = leg_stop_loss_premium_threshold(leg_state)
    leg_state.trail_level = base if base is not None else entry
    leg_state.trail_ref = entry
    return leg_state


def update_leg_trail(leg_state: LegState, premium: float | None = None) -> LegState:
    """Ratchet a short leg's trailing stop downward on favorable premium falls.

    For a short (SELL) leg with a ``Points`` ``LegTrailSL`` of InstrumentMove
    ``I`` and StopLossMove ``S``, every time the premium has fallen a *complete*
    increment of ``I`` points below ``trail_ref`` (the reference at which the
    trail last advanced), the trail level is lowered by ``S`` and the reference
    is advanced downward by ``I``; this repeats for each further complete
    ``I``-point fall (Req 11.2).

    Concretely, with ``px`` the effective premium and
    ``steps = floor((trail_ref - px) / I)`` when that quantity is positive:
        * ``trail_level`` decreases by ``steps * S``.
        * ``trail_ref`` decreases by ``steps * I``.

    Because ``S > 0`` and ``steps`` is only ever applied when positive, the trail
    level is **monotone non-increasing** — it never loosens (Req 11.3). When the
    premium is at or above ``trail_ref`` (no complete fall), the state is left
    unchanged.

    Mutation semantics:
        This function **mutates** ``leg_state.trail_level`` and
        ``leg_state.trail_ref`` in place (ratchet progress is stateful) and
        returns the same ``leg_state``. It no-ops (leaving state untouched) when
        the leg has no ``trail_sl``, the trail has not been initialized
        (``trail_level``/``trail_ref`` still ``None`` — call
        :func:`init_leg_trail` first), the effective premium is unavailable, or
        ``instrument_move`` is not positive.

    Args:
        leg_state: The short leg whose trail is ratcheted. Reads
            ``config.trail_sl``, ``trail_level``, ``trail_ref``, and
            ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        The same ``leg_state`` instance, with the ratchet advanced when one or
        more complete ``I``-point falls have occurred.
    """
    trail = leg_state.config.trail_sl
    if trail is None:
        return leg_state
    if leg_state.trail_level is None or leg_state.trail_ref is None:
        return leg_state

    px = premium if premium is not None else leg_state.last_ltp
    if px is None:
        return leg_state

    instrument_move = trail.instrument_move
    if instrument_move <= 0:
        return leg_state

    fall = leg_state.trail_ref - px
    if fall < instrument_move:
        return leg_state

    steps = math.floor(fall / instrument_move)
    if steps <= 0:
        return leg_state

    leg_state.trail_level -= steps * trail.stoploss_move
    leg_state.trail_ref -= steps * instrument_move
    return leg_state


def leg_trail_triggered(leg_state: LegState, premium: float | None = None) -> bool:
    """Return whether an active trailing stop is breached this cycle (Req 11.4).

    Pure predicate for a short (SELL) leg: the trailing stop triggers a
    square-off when the leg premium rises to or above the current
    ``trail_level`` (**inclusive**). Returns ``False`` when the leg has no
    ``trail_sl``, the trail has not been initialized (``trail_level`` is
    ``None``), or no effective premium is available — matching the fail-safe
    "do not exit on missing data" posture. This function mutates nothing.

    Args:
        leg_state: The short leg to evaluate. Reads ``config.trail_sl``,
            ``trail_level``, and ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        ``True`` when ``premium >= trail_level`` (inclusive), otherwise
        ``False``.
    """
    if leg_state.config.trail_sl is None:
        return False
    level = leg_state.trail_level
    if level is None:
        return False
    px = premium if premium is not None else leg_state.last_ltp
    if px is None:
        return False
    return px >= level


def effective_leg_stop_level(leg_state: LegState) -> float | None:
    """Return the more protective (lower premium) of base SL and trail levels.

    When both a base premium ``LegStopLoss`` and an active ``LegTrailSL`` apply
    to a short leg, the leg is protected at whichever level is the **lower**
    premium — the more protective one — on each monitoring cycle (Req 11.5).
    This helper returns ``min(base_sl_level, trail_level)`` over whichever of the
    two are defined:
        * both defined -> the smaller of the two,
        * only one defined -> that one,
        * neither defined -> ``None``.

    The base level comes from :func:`leg_stop_loss_premium_threshold` (``None``
    for an ``UnderlyingPoints`` SL, a leg with no SL, or a leg with no recorded
    entry fill); the trail level is ``leg_state.trail_level`` (``None`` until
    :func:`init_leg_trail` runs). This is a pure function that mutates nothing.

    Args:
        leg_state: The short leg to evaluate. Reads ``config.stop_loss``,
            ``entry_fill``, and ``trail_level``.

    Returns:
        The effective (lower) premium stop level as a ``float``, or ``None``
        when neither a premium base SL nor a trail level is available.
    """
    base = leg_stop_loss_premium_threshold(leg_state)
    trail = leg_state.trail_level
    candidates = [level for level in (base, trail) if level is not None]
    if not candidates:
        return None
    return min(candidates)


# ---------------------------------------------------------------------------
# Overall stop-loss and trailing stop-loss (Req 13.1, 13.2, 13.3)
# ---------------------------------------------------------------------------
#
# Sign convention (shared with :func:`leg_mtm`/:func:`aggregate_mtm`, Req 13.4):
# aggregate MTM is a **signed rupee** value — a *loss* is negative and a *profit*
# is positive. Requirement 13.1 speaks of an "aggregate MTM loss >= L"; with the
# signed convention that loss magnitude reaching ``L`` is expressed as
# ``mtm <= -L``. Consequently every overall level below is a *signed MTM value*,
# not a loss magnitude:
#
#   * The Overall_Stop_Loss level is ``-L`` (the MTM at which we square off).
#   * The Overall_Trail_SL locked level is likewise a signed MTM value. It is
#     **initialized at the Overall_Stop_Loss level (``-L``)** (Req 13.2) and
#     ratchets *upward* toward 0 and into positive territory as profit is locked
#     in. It is monotone non-decreasing and never loosens.
#
# The ratchet mirrors the leg trailing stop (:func:`update_leg_trail`):
# ``engine_state.peak_mtm`` is the ratcheted reference against which further
# improvement is measured, advancing only by *complete* ``I`` increments (the
# sub-``I`` remainder is retained), so cumulative improvement across many small
# cycles still counts. ``engine_state.locked_mtm_stop`` rises by ``S`` per such
# complete step.


def _initial_locked_mtm_stop(engine_state: EngineState) -> float:
    """Return the signed MTM level at which the overall trail lock initializes.

    The lock initializes at the configured Overall_Stop_Loss level (Req 13.2),
    which in the signed-MTM convention is ``-L`` for an ``OverallStopLoss`` of
    ``L`` rupees. When no Overall_Stop_Loss is configured (an edge case; every
    parameter-table strategy that trails also defines an overall SL), the lock
    falls back to ``0.0`` (breakeven).

    Args:
        engine_state: The engine state carrying the strategy configuration.

    Returns:
        The initial locked MTM stop as a signed ``float``.
    """
    osl = engine_state.config.overall_stop_loss
    if osl is not None:
        return -float(osl.mtm_rupees)
    return 0.0


def overall_stop_loss_triggered(
    engine_state: EngineState,
    mtm: float | None = None,
) -> bool:
    """Return whether the overall MTM stop loss is triggered this cycle (Req 13.1).

    Pure predicate. For an ``OverallStopLoss`` of ``L`` rupees the strategy is
    squared off when the aggregate MTM *loss* reaches ``L`` — i.e. when the
    signed aggregate MTM falls to or below ``-L`` (**inclusive**). When no
    Overall_Stop_Loss is configured the predicate always returns ``False``.

    Args:
        engine_state: The engine state carrying the strategy configuration.
            Reads ``config.overall_stop_loss``.
        mtm: The current signed aggregate MTM in rupees; defaults to
            :func:`aggregate_mtm` over ``engine_state`` when not supplied.

    Returns:
        ``True`` when ``mtm <= -L`` (inclusive), otherwise ``False``.
    """
    osl = engine_state.config.overall_stop_loss
    if osl is None:
        return False
    if mtm is None:
        mtm = aggregate_mtm(engine_state)
    return mtm <= -float(osl.mtm_rupees)


def update_overall_trail(
    engine_state: EngineState,
    mtm: float | None = None,
) -> EngineState:
    """Ratchet the overall trailing-stop lock upward as aggregate MTM improves.

    For a ``Points`` ``OverallTrailSL`` of InstrumentMove ``I`` and StopLossMove
    ``S`` (Req 13.2): the locked MTM stop is initialized at the Overall_Stop_Loss
    level (``-L``; see :func:`_initial_locked_mtm_stop`) on the first call, then
    raised by ``S`` for each *complete* increment of ``I`` by which the aggregate
    MTM improves above its previously recorded peak.

    Concretely, with ``improvement = mtm - engine_state.peak_mtm`` and
    ``steps = floor(improvement / I)`` when ``improvement >= I``:
        * ``locked_mtm_stop`` increases by ``steps * S``.
        * ``peak_mtm`` (the ratcheted reference) increases by ``steps * I``,
          retaining the sub-``I`` remainder so later cycles keep accumulating.

    Because ``S > 0`` and ``steps`` is applied only when positive, both
    ``peak_mtm`` and ``locked_mtm_stop`` are **monotone non-decreasing** — the
    lock never loosens (Req 13.2). When the MTM has not improved by a full ``I``
    above the reference, only first-call initialization occurs and the ratchet is
    left unchanged.

    Mutation semantics:
        This function **mutates** ``engine_state.locked_mtm_stop`` and
        ``engine_state.peak_mtm`` in place (ratchet progress is stateful) and
        returns the same ``engine_state``. It no-ops (leaving both untouched)
        when no ``OverallTrailSL`` is configured. When a trail is configured but
        ``instrument_move`` is not positive, the lock is still initialized (so
        :func:`overall_trail_triggered` has a level) but no stepping occurs.

    Args:
        engine_state: The engine state to ratchet. Reads
            ``config.overall_trail_sl`` and ``config.overall_stop_loss`` and
            mutates ``peak_mtm``/``locked_mtm_stop``.
        mtm: The current signed aggregate MTM in rupees; defaults to
            :func:`aggregate_mtm` over ``engine_state`` when not supplied.

    Returns:
        The same ``engine_state`` instance, with the lock initialized and
        advanced as warranted.
    """
    trail = engine_state.config.overall_trail_sl
    if trail is None:
        return engine_state

    if mtm is None:
        mtm = aggregate_mtm(engine_state)

    # Initialize the locked level at the overall-SL level (-L) on first call.
    if engine_state.locked_mtm_stop is None:
        engine_state.locked_mtm_stop = _initial_locked_mtm_stop(engine_state)

    instrument_move = trail.instrument_move
    if instrument_move <= 0:
        return engine_state

    improvement = mtm - engine_state.peak_mtm
    if improvement < instrument_move:
        return engine_state

    steps = math.floor(improvement / instrument_move)
    if steps <= 0:
        return engine_state

    engine_state.locked_mtm_stop += steps * trail.stoploss_move
    engine_state.peak_mtm += steps * instrument_move
    return engine_state


def overall_trail_triggered(
    engine_state: EngineState,
    mtm: float | None = None,
) -> bool:
    """Return whether the overall trailing-stop lock is breached this cycle.

    Pure predicate (Req 13.3): once the trail lock has been initialized (see
    :func:`update_overall_trail`), the strategy is squared off when the aggregate
    MTM falls to or below the locked level (**inclusive**). Returns ``False``
    when no ``OverallTrailSL`` is configured or the lock has not yet been
    initialized (``locked_mtm_stop`` is ``None``) — matching the fail-safe
    "do not exit on missing data" posture. This function mutates nothing.

    Args:
        engine_state: The engine state carrying the trail configuration and the
            current ``locked_mtm_stop``.
        mtm: The current signed aggregate MTM in rupees; defaults to
            :func:`aggregate_mtm` over ``engine_state`` when not supplied.

    Returns:
        ``True`` when ``mtm <= locked_mtm_stop`` (inclusive), otherwise
        ``False``.
    """
    if engine_state.config.overall_trail_sl is None:
        return False
    locked = engine_state.locked_mtm_stop
    if locked is None:
        return False
    if mtm is None:
        mtm = aggregate_mtm(engine_state)
    return mtm <= locked


# ---------------------------------------------------------------------------
# Momentum-gated entry evaluator (Req 9)
# ---------------------------------------------------------------------------
#
# The two mean-reversion strategies (NF2, SENSEX2) defer a leg's entry until its
# option premium has fallen by a configured ``PointsDown`` amount ``N`` from a
# reference premium captured at Entry_Time (Req 9). The engine marks such a leg
# ``pending_momentum`` and does **not** place it with the immediate legs; instead
# the monitoring loop calls the pure evaluators below on each price update.
#
# Lifecycle of a momentum leg's runtime state:
#   1. At Entry_Time the engine calls :func:`record_momentum_reference` to store
#      ``momentum_ref`` (the leg's LTP) and set ``pending_momentum = True``. When
#      no valid LTP is available the reference cannot be recorded, an error is
#      surfaced, and the leg stays un-entered (Req 9.4).
#   2. While ``pending_momentum`` is True and the current time is inside the
#      entry-exit window, each cycle calls :func:`should_enter_momentum_leg`; the
#      leg is placed only when the LTP has reached ``momentum_ref - N`` and is
#      never placed while the LTP stays above that level (Req 9.2, 9.3).
#   3. If the condition is still unmet at Exit_Time the leg is never entered and
#      the other open legs are squared off (Req 9.5). This pure module exposes
#      :func:`momentum_pending_at_exit` so the exit manager can detect that case;
#      the window/timing and square-off actions themselves live in the
#      monitoring and exit managers.


def is_momentum_leg(leg_state: LegState) -> bool:
    """Return whether the leg is momentum-gated (defines a ``PointsDown``).

    Momentum legs are deferred at Entry_Time rather than placed with the
    immediate legs; the caller uses this predicate to route a leg into the
    momentum-gating path (Req 6.4, 9). This is a pure function that mutates
    nothing.

    Args:
        leg_state: The leg to test. Reads ``config.momentum``.

    Returns:
        ``True`` when the leg has a :class:`LegMomentum` configured, else
        ``False``.
    """
    return leg_state.config.momentum is not None


def record_momentum_reference(
    leg_state: LegState,
    ltp: float | None = None,
) -> bool:
    """Record a momentum leg's reference premium at Entry_Time (Req 9.1, 9.4).

    Captures the leg's LTP as ``momentum_ref`` — the baseline from which the
    required ``PointsDown`` fall is measured — and marks the leg
    ``pending_momentum`` so the monitoring loop keeps evaluating it without
    placing its order yet (Req 9.1, 9.2).

    Missing-data contract (Req 9.4):
        When the effective LTP is unavailable (``ltp`` is ``None`` and the leg
        has no stored ``last_ltp``), the reference cannot be recorded: an error
        is logged, ``momentum_ref`` is left ``None``, ``pending_momentum`` is set
        ``False`` so the leg is never entered, and ``False`` is returned. The
        caller must not place the leg in this case.

    Mutation semantics:
        This function **mutates** ``leg_state.momentum_ref`` and
        ``leg_state.pending_momentum`` in place (momentum gating is inherently
        stateful). It no-ops and returns ``False`` for a non-momentum leg.

    Args:
        leg_state: The momentum leg to arm. Reads ``config.momentum`` and
            ``last_ltp``; writes ``momentum_ref`` and ``pending_momentum``.
        ltp: The leg's current LTP at Entry_Time; defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        ``True`` when the reference premium was recorded and the leg is now
        pending momentum, otherwise ``False``.
    """
    if leg_state.config.momentum is None:
        return False

    reference = ltp if ltp is not None else leg_state.last_ltp
    if reference is None:
        logger.error(
            "Momentum leg %s reference premium unavailable at entry (no valid "
            "LTP); not entering this leg.",
            leg_state.symbol or leg_state.config.offset,
        )
        leg_state.momentum_ref = None
        leg_state.pending_momentum = False
        return False

    leg_state.momentum_ref = reference
    leg_state.pending_momentum = True
    return True


def momentum_entry_threshold(leg_state: LegState) -> float | None:
    """Return the premium level at or below which a momentum leg may enter.

    For a leg with a ``PointsDown`` of ``N`` and a recorded reference premium
    ``R`` (see :func:`record_momentum_reference`), the entry threshold is
    ``R - N`` (Req 9.3). This is a pure function that mutates nothing.

    Args:
        leg_state: The momentum leg. Reads ``config.momentum`` and
            ``momentum_ref``.

    Returns:
        The entry threshold ``R - N`` as a ``float``, or ``None`` when the leg
        has no momentum config or its reference premium has not been recorded.
    """
    momentum = leg_state.config.momentum
    if momentum is None:
        return None
    reference = leg_state.momentum_ref
    if reference is None:
        return None
    return reference - momentum.points_down


def momentum_satisfied(leg_state: LegState, premium: float | None = None) -> bool:
    """Return whether a momentum leg's ``PointsDown`` condition is met (Req 9.3).

    Pure predicate: the condition is satisfied when the leg premium has fallen
    to or below the entry threshold ``momentum_ref - N`` (**inclusive**). Returns
    ``False`` when the threshold is undefined (no momentum config or reference
    not yet recorded) or no effective premium is available — matching the
    fail-safe "do not act on missing data" posture (the leg simply stays
    un-entered). This function mutates nothing.

    Args:
        leg_state: The momentum leg to evaluate. Reads ``config.momentum``,
            ``momentum_ref``, and ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        ``True`` when ``premium <= momentum_ref - N`` (inclusive), otherwise
        ``False``.
    """
    threshold = momentum_entry_threshold(leg_state)
    if threshold is None:
        return False
    px = premium if premium is not None else leg_state.last_ltp
    if px is None:
        return False
    return px <= threshold


def should_enter_momentum_leg(
    leg_state: LegState,
    premium: float | None = None,
) -> bool:
    """Return whether a pending momentum leg should be entered this cycle.

    Pure predicate combining the pending state with the ``PointsDown`` condition
    (Req 9.2, 9.3): a leg is entered only while it is ``pending_momentum`` and
    its premium has reached the ``momentum_ref - N`` threshold. While the premium
    stays above that threshold this returns ``False`` and the leg is never placed
    (Req 9.2). A leg that is not pending (already entered, or whose reference
    could not be recorded per Req 9.4) also returns ``False``.

    The caller (monitoring loop) is responsible for restricting evaluation to the
    entry-exit window; this predicate does not read the clock. It mutates
    nothing.

    Args:
        leg_state: The candidate momentum leg. Reads ``pending_momentum``,
            ``config.momentum``, ``momentum_ref``, and ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        ``True`` when the leg is pending and its momentum condition is met (so
        the caller should place the leg), otherwise ``False``.
    """
    if not leg_state.pending_momentum:
        return False
    return momentum_satisfied(leg_state, premium)


def momentum_pending_at_exit(leg_state: LegState) -> bool:
    """Return whether a momentum leg is still awaiting its trigger at Exit_Time.

    Pure predicate supporting Req 9.5: when a momentum leg's condition remains
    unmet at Exit_Time the leg is never entered, and the exit manager squares off
    the other open legs. A leg is "pending at exit" when it is still
    ``pending_momentum`` and not yet open. This function mutates nothing; the
    timing check and square-off actions live in the exit manager.

    Args:
        leg_state: The momentum leg to test. Reads ``pending_momentum`` and
            ``is_open``.

    Returns:
        ``True`` when the leg is still pending momentum and unopened, otherwise
        ``False``.
    """
    return leg_state.pending_momentum and not leg_state.is_open


# ---------------------------------------------------------------------------
# Re-entry manager (Req 12)
# ---------------------------------------------------------------------------
#
# After a leg is closed by its stop loss (or trailing stop), some strategies
# re-enter the leg (Req 12). Two flavors exist:
#
#   * ``Immediate`` (NF3/SENSEX3, 1DTE): re-enter at the prevailing market price
#     immediately once the stop loss closes the leg (Req 12.1).
#   * ``AtCost`` (NF1/NF2/SENSEX1/SENSEX2): after the stop-loss close, wait until
#     the premium returns to the original entry fill price, then re-enter at that
#     price (Req 12.2).
#
# Both flavors are bounded by the configured re-entry ``count`` (one of 1, 3, 5;
# Req 12.3) and — for the adjustable-strangle strategies (NF3/SENSEX3) that set
# ``reentry_time_restriction_min`` — blocked at or after the 13:59 IST cutoff of
# 09:15 + 284 minutes (Req 12.4). On every re-entry the leg's stop loss and trail
# are re-applied to the fresh position (Req 12.5).
#
# The decision logic is decomposed into small pure predicates
# (:func:`is_valid_reentry_count`, :func:`reentry_count_remaining`,
# :func:`reentry_cutoff_time`, :func:`reentry_time_allowed`,
# :func:`atcost_reentry_ready`) so it can be property-tested without a client,
# while :func:`maybe_reenter` performs the actual re-entry order and the leg
# state mutation. The *decision to invoke* ``maybe_reenter`` (i.e. only after an
# SL/trail close, and never after a scheduled exit or an overall square-off)
# belongs to the monitoring/exit dispatcher; ``maybe_reenter`` itself is safe to
# call repeatedly on a stopped leg — it returns ``False`` until the flavor's
# condition is met and once more after the leg is re-opened.

#: IST market open used as the baseline for the re-entry time restriction
#: (Req 12.4, decision A5): the cutoff is this time plus
#: ``reentry_time_restriction_min`` minutes.
MARKET_OPEN_IST = time(9, 15)

#: The re-entry counts the backtest permits per leg (Req 12.3).
VALID_REENTRY_COUNTS = (1, 3, 5)


def is_valid_reentry_count(count: object) -> bool:
    """Return whether ``count`` is a permitted re-entry count (one of 1, 3, 5).

    Enforces Req 12.3's constraint that the configured re-entry count is one of
    1, 3, or 5. ``bool`` is rejected even though it subclasses ``int``. This is a
    pure function that mutates nothing.

    Args:
        count: The configured re-entry count to check.

    Returns:
        ``True`` when ``count`` is exactly 1, 3, or 5, otherwise ``False``.
    """
    if isinstance(count, bool):
        return False
    return isinstance(count, int) and count in VALID_REENTRY_COUNTS


def reentry_count_remaining(leg_state: LegState) -> int:
    """Return how many further re-entries the leg is still permitted (Req 12.3).

    Computes ``configured_count - reentries_done``, clamped at zero, so completed
    re-entries can never exceed the configured count. Returns ``0`` when the leg
    has no re-entry configured or its configured count is not a permitted value
    (one of 1, 3, 5). This is a pure function that mutates nothing.

    Args:
        leg_state: The leg to inspect. Reads ``config.reentry`` and
            ``reentries_done``.

    Returns:
        The number of re-entries still allowed as a non-negative ``int``.
    """
    reentry = leg_state.config.reentry
    if reentry is None or not is_valid_reentry_count(reentry.count):
        return 0
    return max(0, reentry.count - leg_state.reentries_done)


def reentry_cutoff_time(restriction_min: int) -> time:
    """Return the time-of-day cutoff = 09:15 IST + ``restriction_min`` minutes.

    For the confirmed value ``284`` this yields ``13:59`` (Req 12.4, decision
    A5). This is a pure function that mutates nothing.

    Args:
        restriction_min: Minutes after the 09:15 IST market open at which
            re-entries become disallowed.

    Returns:
        The cutoff as a :class:`datetime.time`.
    """
    base = datetime.combine(date.min, MARKET_OPEN_IST) + timedelta(
        minutes=restriction_min
    )
    return base.time()


def reentry_time_allowed(
    config: StrategyConfig,
    now: time | datetime | None = None,
) -> bool:
    """Return whether re-entries are still permitted at the current time (Req 12.4).

    When the strategy sets ``reentry_time_restriction_min`` (NF3/SENSEX3), no
    re-entry may be placed at or after the cutoff of 09:15 IST +
    ``reentry_time_restriction_min`` minutes; the boundary itself is disallowed
    ("at or after"). Strategies that leave the restriction unset (``None``) are
    always allowed. This is a pure function that mutates nothing.

    Args:
        config: The strategy configuration. Reads
            ``reentry_time_restriction_min``.
        now: The current IST time, as a :class:`datetime.time` or
            :class:`datetime.datetime` (its time-of-day is used). Defaults to the
            current IST wall-clock time; injectable so tests can pin the boundary.

    Returns:
        ``True`` when a re-entry may be placed now (strictly before the cutoff or
        no restriction configured), otherwise ``False``.
    """
    restriction = config.reentry_time_restriction_min
    if restriction is None:
        return True
    cutoff = reentry_cutoff_time(restriction)
    if now is None:
        now_time = datetime.now(_IST).time()
    elif isinstance(now, datetime):
        now_time = now.time()
    else:
        now_time = now
    return now_time < cutoff


def atcost_reentry_ready(leg_state: LegState, premium: float | None = None) -> bool:
    """Return whether an ``AtCost`` leg's premium has returned to its fill (Req 12.2).

    After a short leg's stop loss closes it (its premium having risen away from
    the fill), an ``AtCost`` re-entry is placed only once the premium falls back
    to or below the original entry fill price. This predicate reports that the
    premium has "returned to cost" (``premium <= entry_fill``, inclusive).
    Returns ``False`` for a non-``AtCost`` leg, or when the entry fill or the
    effective premium is unavailable — matching the module's fail-safe
    "do not act on missing data" posture. This is a pure function that mutates
    nothing.

    Args:
        leg_state: The stopped ``AtCost`` leg. Reads ``config.reentry``,
            ``entry_fill``, and ``last_ltp``.
        premium: The current leg premium (LTP); defaults to
            ``leg_state.last_ltp`` when not supplied.

    Returns:
        ``True`` when the premium has returned to or below the original entry
        fill price, otherwise ``False``.
    """
    reentry = leg_state.config.reentry
    if reentry is None or ReentryKind(reentry.kind) is not ReentryKind.AT_COST:
        return False
    entry = leg_state.entry_fill
    px = premium if premium is not None else leg_state.last_ltp
    if entry is None or px is None:
        return False
    return px <= entry


def _place_reentry_order(
    engine_state: EngineState,
    leg_state: LegState,
    price_type: str,
    price: float | None,
) -> tuple[str | None, float | None]:
    """Place a single-leg re-entry order and return ``(order_id, avg_fill)``.

    Submits the leg's recorded option ``symbol`` back with its original action
    (SELL for shorts) on the index F&O exchange, then reads the average fill via
    ``client.orderstatus``. A ``MARKET`` order is used for an ``Immediate``
    re-entry; a ``LIMIT`` order at ``price`` for an ``AtCost`` re-entry.

    Args:
        engine_state: The engine state (supplies the client, strategy name,
            quantity, product, and index exchange).
        leg_state: The leg being re-entered (supplies ``symbol`` and action).
        price_type: ``"MARKET"`` or ``"LIMIT"``.
        price: The limit price for an ``AtCost`` re-entry, else ``None``.

    Returns:
        A ``(order_id, avg_fill)`` tuple. Either element may be ``None`` when the
        SDK response omits it.
    """
    config = engine_state.config
    client = engine_state.client
    response = client.placeorder(
        strategy=config.strategy_name,
        symbol=leg_state.symbol,
        action=Action(leg_state.config.action).value,
        exchange=config.index.fno_exchange,
        price_type=price_type,
        product=config.product,
        quantity=str(config.quantity),
        price=str(price) if price is not None else "0",
    )

    order_id = None
    if isinstance(response, dict) and response.get("status") == "success":
        order_id = response.get("orderid")

    avg_fill: float | None = None
    if order_id is not None:
        status = client.orderstatus(order_id=order_id, strategy=config.strategy_name)
        if isinstance(status, dict) and status.get("status") == "success":
            raw = (status.get("data") or {}).get("average_price")
            if raw is not None:
                try:
                    avg_fill = float(raw)
                except (TypeError, ValueError):
                    avg_fill = None
    return order_id, avg_fill


def maybe_reenter(
    engine_state: EngineState,
    leg_state: LegState,
    market_price: float | None = None,
    spot: float | None = None,
    now: time | datetime | None = None,
) -> bool:
    """Re-enter a stopped leg per its Immediate/AtCost rules (Req 12).

    Intended to be called by the monitoring/exit dispatcher after a leg has been
    closed by its stop loss or trailing stop. It is safe to call every cycle: it
    performs a re-entry only when all of the following hold, and otherwise
    returns ``False`` without side effects:

        * the leg has a :class:`LegReentry` configured and is currently closed
          (``not is_open``) with a recorded original ``entry_fill``;
        * the configured count is a permitted value (1, 3, or 5) and completed
          re-entries remain below it (Req 12.3);
        * the current time is strictly before the re-entry cutoff for strategies
          that set one (NF3/SENSEX3 at 13:59 IST) (Req 12.4);
        * for ``AtCost``, the premium has returned to or below the original entry
          fill price (Req 12.2); ``Immediate`` re-enters unconditionally at the
          prevailing market price (Req 12.1).

    On a successful re-entry it places the order (see
    :func:`_place_reentry_order`), then updates the leg's runtime state: the leg
    is marked open, ``reentries_done`` is incremented, ``exit_reason`` is
    cleared, and the leg's stop loss and trail are re-applied to the fresh
    position (Req 12.5) — ``Immediate`` adopts the prevailing market price as the
    new ``entry_fill`` (and, when ``spot`` is supplied, refreshes the
    ``UnderlyingPoints`` baseline), while ``AtCost`` retains the original
    ``entry_fill``. The trailing-stop ratchet is re-initialized via
    :func:`init_leg_trail`.

    Mutation semantics:
        On re-entry this **mutates** ``leg_state`` (``is_open``, ``entry_fill``,
        ``entry_spot``, ``order_id``, ``reentries_done``, ``exit_reason``, and
        the trail fields) and returns ``True``. When no re-entry is warranted it
        mutates nothing and returns ``False``. An invalid configured count is
        logged and blocks re-entry (returns ``False``) rather than raising, to
        avoid disrupting the monitoring loop.

    Args:
        engine_state: The engine state (client, strategy config, quantity).
        leg_state: The stopped leg to consider for re-entry.
        market_price: The prevailing market premium for an ``Immediate``
            re-entry; used as the fallback new ``entry_fill`` when the SDK does
            not report an average fill. Defaults to ``leg_state.last_ltp``.
        spot: The current underlying spot; when supplied, refreshes an
            ``Immediate`` leg's ``UnderlyingPoints`` entry-spot baseline.
        now: The current IST time for the cutoff check (see
            :func:`reentry_time_allowed`); defaults to the IST wall clock.

    Returns:
        ``True`` when the leg was re-entered this call, otherwise ``False``.
    """
    reentry = leg_state.config.reentry
    if reentry is None:
        return False

    # Already open (e.g. called again after a successful re-entry) → nothing to do.
    if leg_state.is_open:
        return False

    # A re-entry references the original position's fill; require one.
    if leg_state.entry_fill is None:
        return False

    # Enforce the permitted count set (Req 12.3); invalid counts block re-entry.
    if not is_valid_reentry_count(reentry.count):
        logger.error(
            "Leg %s has invalid re-entry count %r (must be one of %s); "
            "not re-entering.",
            leg_state.symbol or leg_state.config.offset,
            reentry.count,
            list(VALID_REENTRY_COUNTS),
        )
        return False

    # Bound the number of re-entries (Req 12.3).
    if reentry_count_remaining(leg_state) <= 0:
        return False

    # Enforce the 13:59 IST cutoff for NF3/SENSEX3 (Req 12.4).
    if not reentry_time_allowed(engine_state.config, now):
        logger.info(
            "Re-entry for leg %s blocked: at/after the %s IST cutoff.",
            leg_state.symbol or leg_state.config.offset,
            reentry_cutoff_time(engine_state.config.reentry_time_restriction_min),
        )
        return False

    kind = ReentryKind(reentry.kind)
    original_fill = leg_state.entry_fill

    if kind is ReentryKind.IMMEDIATE:
        # Re-enter at the prevailing market price immediately (Req 12.1).
        price = market_price if market_price is not None else leg_state.last_ltp
        order_id, avg_fill = _place_reentry_order(
            engine_state, leg_state, price_type="MARKET", price=None
        )
        new_fill = avg_fill if avg_fill is not None else price
        if new_fill is None:
            new_fill = original_fill
        leg_state.entry_fill = new_fill
        if spot is not None:
            leg_state.entry_spot = spot
        log_price = new_fill
    else:
        # AtCost: only when the premium has returned to the original fill (Req 12.2).
        if not atcost_reentry_ready(leg_state, market_price):
            return False
        order_id, avg_fill = _place_reentry_order(
            engine_state, leg_state, price_type="LIMIT", price=original_fill
        )
        # Re-enter at the original entry fill price; the baseline is unchanged.
        leg_state.entry_fill = original_fill
        log_price = original_fill

    # Common post-re-entry state updates.
    leg_state.order_id = order_id
    leg_state.is_open = True
    leg_state.exit_reason = None
    leg_state.reentries_done += 1

    # Re-apply the leg's trailing stop to the fresh position (Req 12.5). The base
    # stop loss is re-applied implicitly because the per-cycle evaluators read
    # ``config.stop_loss`` against the (updated) ``entry_fill``/``entry_spot``.
    leg_state.trail_level = None
    leg_state.trail_ref = None
    init_leg_trail(leg_state)

    logger.info(
        "Re-entered leg %s (%s) action=%s qty=%d price=%s count=%d/%d",
        leg_state.symbol or leg_state.config.offset,
        kind.value,
        Action(leg_state.config.action).value,
        engine_state.config.quantity,
        log_price,
        leg_state.reentries_done,
        reentry.count,
    )
    return True


# ---------------------------------------------------------------------------
# Risk-evaluation precedence dispatcher (Req 14.7)
# ---------------------------------------------------------------------------
#
# On each monitoring cycle the engine evaluates the five risk conditions in a
# single strict precedence order and acts on the *earliest* satisfied one
# (Req 14.7, Property 23):
#
#     momentum entry
#         -> leg stop loss
#             -> leg trailing stop
#                 -> overall stop loss
#                     -> overall trailing stop
#
# The dispatcher is a **pure decision function**: it inspects the current engine
# and leg state (leg premiums via each leg's ``last_ltp``, the underlying
# ``spot`` for ``UnderlyingPoints`` legs, and the signed aggregate MTM) using the
# existing per-leg and portfolio predicates, and returns the single
# highest-precedence :class:`RiskDecision` for the caller (the monitoring loop,
# task 18) to act on. It mutates nothing, so the trail ratchets
# (:func:`update_leg_trail`, :func:`update_overall_trail`) must be advanced by
# the loop earlier in the cycle.
#
# The three per-leg categories (momentum entry, leg stop loss, leg trailing
# stop) are scanned across all legs in configured order, but *category*
# precedence is absolute: a leg stop loss on any leg outranks a leg trailing
# stop on any other leg. Within a category the first matching leg (in configured
# order) is chosen so the decision is deterministic.


class RiskAction(str, Enum):
    """The kind of action a monitoring cycle may take, in precedence order.

    The member order matches the strict evaluation precedence of Req 14.7 and is
    captured explicitly in :data:`RISK_PRECEDENCE`.
    """

    MOMENTUM_ENTRY = "momentum_entry"
    LEG_STOP_LOSS = "leg_stop_loss"
    LEG_TRAIL = "leg_trail"
    OVERALL_STOP_LOSS = "overall_stop_loss"
    OVERALL_TRAIL = "overall_trail"


#: The strict precedence order in which risk conditions are evaluated each
#: monitoring cycle (Req 14.7). Earlier entries win when several conditions are
#: simultaneously satisfied (Property 23).
RISK_PRECEDENCE: tuple[RiskAction, ...] = (
    RiskAction.MOMENTUM_ENTRY,
    RiskAction.LEG_STOP_LOSS,
    RiskAction.LEG_TRAIL,
    RiskAction.OVERALL_STOP_LOSS,
    RiskAction.OVERALL_TRAIL,
)


@dataclass
class RiskDecision:
    """The single highest-precedence action selected for a monitoring cycle.

    Attributes:
        action: Which risk condition fired (see :class:`RiskAction`).
        leg_state: The leg the action applies to for the per-leg conditions
            (momentum entry, leg stop loss, leg trailing stop); ``None`` for the
            portfolio-wide overall stop-loss / overall trailing-stop conditions.
        mtm: The signed aggregate MTM (rupees) evaluated this cycle for the
            overall conditions; ``None`` for the per-leg conditions.
    """

    action: RiskAction
    leg_state: LegState | None = None
    mtm: float | None = None


def evaluate_risk_cycle(
    engine_state: EngineState,
    spot: float | None = None,
    mtm: float | None = None,
    evaluate_overall: bool = True,
) -> RiskDecision | None:
    """Return the highest-precedence risk action for this cycle, or ``None``.

    Evaluates the five risk conditions in the strict precedence order of
    :data:`RISK_PRECEDENCE` (Req 14.7) and returns a :class:`RiskDecision` for
    the *earliest* satisfied condition, so that when two or more conditions are
    simultaneously satisfiable the action taken corresponds to the earliest in
    the order momentum entry -> leg stop loss -> leg trailing stop -> overall
    stop loss -> overall trailing stop (Property 23). Returns ``None`` when no
    condition is satisfied this cycle.

    Evaluation detail per category (delegating to the existing pure predicates):
        1. **Momentum entry** — the first ``pending_momentum`` leg (in configured
           order) whose ``PointsDown`` fall condition is met
           (:func:`should_enter_momentum_leg`), so the caller places it
           (Req 9.2, 9.3).
        2. **Leg stop loss** — the first *open* leg whose stop loss is triggered
           (:func:`leg_stop_loss_triggered`); ``spot`` is forwarded for
           ``UnderlyingPoints`` legs (Req 10).
        3. **Leg trailing stop** — the first *open* leg whose active trail level
           is breached (:func:`leg_trail_triggered`) (Req 11.4).
        4. **Overall stop loss** — the signed aggregate MTM breaching ``-L``
           (:func:`overall_stop_loss_triggered`) (Req 13.1).
        5. **Overall trailing stop** — the aggregate MTM breaching the locked
           trail level (:func:`overall_trail_triggered`) (Req 13.3).

    Only open legs are considered for the leg stop-loss and leg trailing-stop
    categories: a leg already closed by a prior trigger must not fire again. The
    momentum-entry predicate already restricts itself to pending (un-opened)
    legs.

    This is a pure function that mutates nothing; the caller is responsible for
    having advanced the trail ratchets (:func:`update_leg_trail`,
    :func:`update_overall_trail`) earlier in the cycle and for performing the
    action the returned decision names.

    Args:
        engine_state: The current engine state (legs and portfolio config).
        spot: The current underlying spot, forwarded to the leg stop-loss
            evaluator for ``UnderlyingPoints`` legs; ``None`` when unavailable.
        mtm: The signed aggregate MTM in rupees for the overall conditions;
            defaults to :func:`aggregate_mtm` over ``engine_state`` when ``None``
            and the overall conditions are evaluated.
        evaluate_overall: When ``False``, the two portfolio-wide overall
            conditions are skipped this cycle — used by the monitoring loop to
            honour the "skip aggregate MTM on stale data" rule (Req 13.6) without
            abandoning the per-leg checks.

    Returns:
        The highest-precedence :class:`RiskDecision`, or ``None`` when no risk
        condition fired this cycle.
    """
    # 1. Momentum entry (per-leg): place a pending momentum leg once its
    #    required fall has occurred (Req 9.2, 9.3).
    for leg in engine_state.legs:
        if should_enter_momentum_leg(leg):
            return RiskDecision(RiskAction.MOMENTUM_ENTRY, leg_state=leg)

    # 2. Leg stop loss (per-leg): only meaningful for currently open legs
    #    (Req 10).
    for leg in engine_state.legs:
        if leg.is_open and leg_stop_loss_triggered(leg, spot=spot):
            return RiskDecision(RiskAction.LEG_STOP_LOSS, leg_state=leg)

    # 3. Leg trailing stop (per-leg): only meaningful for currently open legs
    #    (Req 11.4).
    for leg in engine_state.legs:
        if leg.is_open and leg_trail_triggered(leg):
            return RiskDecision(RiskAction.LEG_TRAIL, leg_state=leg)

    # The two overall conditions are portfolio-wide; the loop skips them when the
    # aggregate MTM cannot be trusted this cycle (stale data, Req 13.6).
    if not evaluate_overall:
        return None

    if mtm is None:
        mtm = aggregate_mtm(engine_state)

    # 4. Overall stop loss (Req 13.1).
    if overall_stop_loss_triggered(engine_state, mtm):
        return RiskDecision(RiskAction.OVERALL_STOP_LOSS, mtm=mtm)

    # 5. Overall trailing stop (Req 13.3).
    if overall_trail_triggered(engine_state, mtm):
        return RiskDecision(RiskAction.OVERALL_TRAIL, mtm=mtm)

    return None


# ---------------------------------------------------------------------------
# Bounded retry helper (Req 6.5, 7.6, 10.6)
# ---------------------------------------------------------------------------
#
# Three SDK-facing operations must be retried a bounded number of times and stop
# on the first success:
#
#     * entry-leg placement            (Req 6.5)
#     * average-fill retrieval          (Req 7.6, with a 2s inter-attempt wait)
#     * leg / overall square-off        (Req 10.6)
#
# They share the same control shape, so the retry policy lives in one place:
# :func:`run_with_retry` runs a caller-supplied ``operation`` up to
# :data:`MAX_RETRY_ATTEMPTS` times, treating an attempt as a success when the
# caller's ``is_success`` predicate accepts its return value (and as a failure
# when it raises). It returns on the *first* success without making further
# attempts, and never exceeds the cap (Property 11).
#
# The helper is intentionally state-free: it does not touch ``EngineState`` or
# ``LegState``. Callers keep ownership of their own already-placed / recorded
# state (traded symbol, order id, fills), so a later failed attempt can never
# discard an earlier success — the helper only decides *whether to attempt
# again* and reports the outcome.


@dataclass
class RetryResult:
    """Outcome of a :func:`run_with_retry` call.

    Attributes:
        succeeded: Whether some attempt's result satisfied the success
            predicate.
        value: The return value of the first successful attempt, or the return
            value of the final attempt when none succeeded (``None`` when every
            attempt raised).
        attempts: The number of attempts actually made; always between 1 and the
            configured maximum inclusive and never exceeding it (Property 11).
        errors: The exceptions raised by attempts, in attempt order; empty when
            no attempt raised.
    """

    succeeded: bool
    value: Any
    attempts: int
    errors: list[Exception] = field(default_factory=list)


def _default_is_success(value: Any) -> bool:
    """Default success predicate: any non-``None`` result is a success."""
    return value is not None


def run_with_retry(
    operation: Callable[[], Any],
    *,
    is_success: Callable[[Any], bool] | None = None,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    delay: float = 0.0,
    sleep: Callable[[float], None] = _sleep,
    description: str = "operation",
) -> RetryResult:
    """Run ``operation`` up to ``max_attempts`` times, stopping on first success.

    This is the single shared bounded-retry policy behind entry-leg placement
    (Req 6.5), average-fill retrieval (Req 7.6), and square-off (Req 10.6). Each
    attempt calls ``operation()``; the attempt succeeds when ``is_success``
    accepts its return value and fails when ``operation`` raises (the exception
    is captured, not propagated) or when the predicate rejects the value. On the
    first success the function returns immediately without further attempts; the
    total number of attempts never exceeds ``max_attempts`` (Property 11).

    The helper holds no engine or leg state: preserving already-placed or
    recorded data across a failed retry is the caller's responsibility, and
    because this function only re-invokes ``operation`` it can never overwrite an
    earlier success.

    Args:
        operation: A zero-argument callable performing one attempt (e.g. a
            single ``placeorder`` / ``orderstatus`` / square-off SDK call).
        is_success: Predicate deciding whether an attempt's return value counts
            as success. Defaults to "any non-``None`` value" via
            :func:`_default_is_success`.
        max_attempts: Maximum total attempts, capped at :data:`MAX_RETRY_ATTEMPTS`
            when a larger value is passed and floored at 1 when a smaller value
            is passed, so the three-attempt bound always holds (Property 11).
        delay: Seconds to wait between attempts (e.g.
            :data:`AVG_FILL_RETRY_INTERVAL` for average-fill retrieval, Req 7.6).
            No wait occurs after the final attempt or after a success.
        sleep: The sleep callable used for ``delay`` waits; injectable so tests
            avoid real delays.
        description: A short label used in the retry-exhausted log message.

    Returns:
        A :class:`RetryResult` describing whether the operation succeeded, the
        relevant return value, the number of attempts made, and any exceptions
        raised along the way.
    """
    predicate = is_success if is_success is not None else _default_is_success
    # Clamp to [1, MAX_RETRY_ATTEMPTS] so the bound holds regardless of caller.
    attempts_cap = max(1, min(int(max_attempts), MAX_RETRY_ATTEMPTS))

    last_value: Any = None
    errors: list[Exception] = []

    for attempt in range(1, attempts_cap + 1):
        try:
            value = operation()
        except Exception as exc:  # noqa: BLE001 - retries must survive any SDK error
            errors.append(exc)
            logger.error(
                "%s attempt %d/%d raised: %s",
                description,
                attempt,
                attempts_cap,
                exc,
            )
        else:
            last_value = value
            if predicate(value):
                return RetryResult(
                    succeeded=True,
                    value=value,
                    attempts=attempt,
                    errors=errors,
                )

        if attempt < attempts_cap and delay > 0:
            sleep(delay)

    logger.error(
        "%s failed after %d attempt(s); giving up.", description, attempts_cap
    )
    return RetryResult(
        succeeded=False,
        value=last_value,
        attempts=attempts_cap,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Entry executor (Req 6, 7, 8, 17)
# ---------------------------------------------------------------------------
#
# ``place_entry`` places every non-momentum leg in a single ``optionsmultiorder``
# call, records each leg's traded symbol / order id, and captures its average
# fill via ``orderstatus`` so downstream risk controls have an entry price. It
# reuses the shared bounded-retry policy (:func:`run_with_retry`) for both the
# placement (Req 6.5) and the average-fill retrieval (Req 7.6), and the
# :func:`is_momentum_leg` predicate to defer momentum-gated legs to the
# monitoring loop (Req 6.4, 9). The one-entry-per-day rule (MaxPositionInADay = 1,
# Req 6.3) is enforced via ``EngineState.entered_today``.


#: Price type sent for entry legs; the backtest enters at market (Req 7.1).
ENTRY_PRICE_TYPE = "MARKET"

#: Default product applied when a strategy configures none (Req 7.3).
DEFAULT_PRODUCT = "NRML"


def _resolve_action(action: Action | str) -> Action | None:
    """Return the leg's resolved :class:`Action`, or ``None`` when invalid.

    Accepts either an :class:`Action` member or a ``"BUY"``/``"SELL"`` string.
    Anything that does not resolve to one of the two supported sides yields
    ``None`` so the caller can reject the entry with a logged error (Req 7.2).
    """
    if isinstance(action, Action):
        resolved = action
    else:
        try:
            resolved = Action(action)
        except (ValueError, TypeError):
            return None
    return resolved if resolved in (Action.BUY, Action.SELL) else None


def _optionsmultiorder_succeeded(response: Any) -> bool:
    """Return whether an ``optionsmultiorder`` response reports success."""
    return isinstance(response, dict) and response.get("status") == "success"


def _orderstatus_average_fill(status: Any) -> float | None:
    """Extract a non-null average fill price from an ``orderstatus`` response.

    Returns the parsed ``data.average_price`` as a ``float`` when the response is
    a success envelope carrying a numeric price, otherwise ``None`` (mirrors the
    parsing used by :func:`_place_reentry_order`).
    """
    if not isinstance(status, dict) or status.get("status") != "success":
        return None
    raw = (status.get("data") or {}).get("average_price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _build_entry_leg_payload(
    leg_state: LegState,
    config: StrategyConfig,
    expiry: str | None,
    product: str,
) -> dict[str, Any]:
    """Build one leg's ``optionsmultiorder`` payload entry (Req 7.1).

    Mirrors the SDK leg shape from ``examples/python/straddle_with_stops.py``:
    the per-leg ``offset``, ``option_type``, ``action``, ``quantity``,
    ``product``, ``pricetype``, and ``splitsize``. The resolved weekly
    ``expiry_date`` is attached when known so all legs share one expiry.
    """
    payload: dict[str, Any] = {
        "offset": leg_state.config.offset,
        "option_type": OptionType(leg_state.config.option_type).value,
        "action": Action(leg_state.config.action).value,
        "quantity": config.quantity,
        "product": product,
        "pricetype": ENTRY_PRICE_TYPE,
        "splitsize": 0,
    }
    if expiry is not None:
        payload["expiry_date"] = expiry
    return payload


def _capture_leg_fill(
    engine_state: EngineState,
    leg_state: LegState,
    leg_result: Any,
    *,
    spot: float | None,
    sleep: Callable[[float], None],
) -> None:
    """Record a placed leg's symbol/order id and capture its average fill.

    Applies the per-leg result contract of Req 7.4-7.8 to ``leg_state``:

        * Records whatever ``symbol`` / ``orderid`` the response supplies so
          partial data is retained (Req 7.5).
        * When either the traded symbol or the order id is missing, the leg is
          treated as failed: an error naming the missing field is logged, the
          leg is left closed, and no fill is fetched (Req 7.5). Other legs'
          recorded data is untouched (Req 7.8).
        * Otherwise the leg is marked open, its ``entry_spot`` baseline is
          recorded when ``spot`` is known (the ``UnderlyingPoints`` reference),
          and the average fill is fetched via ``orderstatus`` with a bounded
          retry of up to 3 attempts at a 2-second interval (Req 7.6).
        * If no non-null average fill is returned after 3 attempts, the failure
          is logged and the leg is excluded from risk calculations
          (``entry_fill`` stays ``None``) while its symbol and order id are
          preserved (Req 7.7); the trailing stop is initialized only once a fill
          is known (Req 11.1).

    Mutates ``leg_state`` in place; returns nothing.
    """
    config = engine_state.config
    result = leg_result if isinstance(leg_result, dict) else {}
    symbol = result.get("symbol")
    order_id = result.get("orderid")

    # Retain any partial data the response did supply (Req 7.5).
    if symbol:
        leg_state.symbol = symbol
    if order_id is not None and order_id != "":
        leg_state.order_id = str(order_id)

    if not symbol or order_id is None or order_id == "":
        missing = []
        if not symbol:
            missing.append("symbol")
        if order_id is None or order_id == "":
            missing.append("orderid")
        logger.error(
            "%s entry leg %s failed: order response missing %s; retaining other "
            "leg data.",
            config.strategy_name,
            leg_state.config.offset,
            " and ".join(missing),
        )
        leg_state.is_open = False
        return

    # Leg placed successfully: an open position now exists for it.
    leg_state.is_open = True
    if spot is not None:
        leg_state.entry_spot = spot

    fill = run_with_retry(
        lambda: engine_state.client.orderstatus(
            order_id=leg_state.order_id, strategy=config.strategy_name
        ),
        is_success=lambda status: _orderstatus_average_fill(status) is not None,
        delay=AVG_FILL_RETRY_INTERVAL,
        sleep=sleep,
        description=(
            f"{config.strategy_name} leg {leg_state.symbol} average-fill retrieval"
        ),
    )

    avg_fill = _orderstatus_average_fill(fill.value) if fill.succeeded else None
    if avg_fill is None:
        logger.error(
            "%s entry leg %s: no average fill after %d attempt(s); excluding leg "
            "from risk calculations but keeping symbol/orderid.",
            config.strategy_name,
            leg_state.symbol,
            fill.attempts,
        )
        leg_state.entry_fill = None
        return

    leg_state.entry_fill = avg_fill
    init_leg_trail(leg_state)
    logger.info(
        "%s entered leg %s (%s %s) qty=%d fill=%.2f",
        config.strategy_name,
        leg_state.symbol,
        Action(leg_state.config.action).value,
        OptionType(leg_state.config.option_type).value,
        config.quantity,
        avg_fill,
    )


def place_entry(
    engine_state: EngineState,
    *,
    spot: float | None = None,
    sleep: Callable[[float], None] = _sleep,
) -> bool:
    """Place the strategy's non-momentum entry legs and capture their fills.

    Executes the scheduled entry (Req 6, 7): it places every non-momentum leg in
    a single ``optionsmultiorder`` call, records each leg's traded symbol and
    order id, and captures its average fill for use by the stop-loss/re-entry
    calculations. Momentum-gated legs (:func:`is_momentum_leg`) are **deferred**
    to the monitoring loop (Req 6.4, 9) — they are marked ``pending_momentum``
    here and never included in the entry order.

    One-entry-per-day (Req 6.3): when ``engine_state.entered_today`` is already
    set the trigger is rejected and the existing position is left unchanged,
    honouring MaxPositionInADay = 1. On the first successful placement the flag
    is set so later triggers are no-ops.

    Order construction (Req 7.1-7.3):
        * ``underlying`` = index name, ``exchange`` = index spot exchange (e.g.
          ``NSE_INDEX``), and the resolved weekly ``expiry_date`` are passed to
          ``optionsmultiorder`` alongside the per-leg payloads.
        * Each leg carries its ``offset``, ``option_type``, resolved ``action``
          (SELL for shorts, BUY for hedges), the strategy ``quantity``
          (``lots * lot_size``), the configured ``product`` (or ``NRML`` when
          none is set), and a ``MARKET`` price type.
        * If any leg's resolved action is neither SELL nor BUY, the entire entry
          is rejected with a logged error and no order is placed (Req 7.2).

    Resilience:
        The placement is wrapped in :func:`run_with_retry` (bounded to 3
        attempts, Req 6.5). Per-leg result handling and average-fill retrieval
        follow :func:`_capture_leg_fill` (Req 7.4-7.8).

    Args:
        engine_state: The engine state. Reads ``config``, ``client``, and
            ``expiry``; builds/populates ``legs`` and sets ``entered_today``.
        spot: The underlying spot at entry, recorded as each filled leg's
            ``entry_spot`` baseline for ``UnderlyingPoints`` stop losses; may be
            ``None`` when unavailable.
        sleep: Sleep callable used for the average-fill retry interval;
            injectable so tests avoid real delays.

    Returns:
        ``True`` when an entry was placed this call (a position now exists for
        the day, even if some individual legs failed), otherwise ``False`` (the
        trigger was rejected for MaxPositionInADay, an invalid action, or a
        fully failed placement).
    """
    config = engine_state.config
    client = engine_state.client

    # Req 6.3: honour MaxPositionInADay = 1 — reject repeat triggers.
    if engine_state.entered_today:
        logger.warning(
            "%s already holds an entry position for today; ignoring additional "
            "entry trigger (MaxPositionInADay=1).",
            config.strategy_name,
        )
        return False

    # Build per-leg runtime state once (idempotent across retriggers).
    if not engine_state.legs:
        engine_state.legs = [LegState(config=leg) for leg in config.legs]

    # Req 7.2: reject the whole entry if any resolved action is not SELL/BUY.
    for leg_state in engine_state.legs:
        if _resolve_action(leg_state.config.action) is None:
            logger.error(
                "%s entry rejected: leg %s has invalid action %r (must be SELL or "
                "BUY).",
                config.strategy_name,
                leg_state.config.offset,
                leg_state.config.action,
            )
            return False

    product = config.product or DEFAULT_PRODUCT

    immediate_legs = [ls for ls in engine_state.legs if not is_momentum_leg(ls)]
    momentum_legs = [ls for ls in engine_state.legs if is_momentum_leg(ls)]

    # Defer momentum-gated legs to the monitoring loop (Req 6.4, 9).
    for leg_state in momentum_legs:
        leg_state.pending_momentum = True
        logger.info(
            "%s deferring momentum leg %s %s to the monitoring loop.",
            config.strategy_name,
            OptionType(leg_state.config.option_type).value,
            leg_state.config.offset,
        )

    if not immediate_legs:
        # Every leg is momentum-gated; the position is deferred but the day's
        # entry has been initiated, so honour MaxPositionInADay.
        engine_state.entered_today = True
        return True

    legs_payload = [
        _build_entry_leg_payload(ls, config, engine_state.expiry, product)
        for ls in immediate_legs
    ]

    placement = run_with_retry(
        lambda: client.optionsmultiorder(
            strategy=config.strategy_name,
            underlying=config.index.name,
            exchange=config.index.index_exchange,
            legs=legs_payload,
        ),
        is_success=_optionsmultiorder_succeeded,
        sleep=sleep,
        description=f"{config.strategy_name} entry placement",
    )

    if not placement.succeeded:
        logger.error(
            "%s entry placement failed after %d attempt(s); no legs placed.",
            config.strategy_name,
            placement.attempts,
        )
        return False

    # A position now exists for the trading day (Req 6.3).
    engine_state.entered_today = True

    results = placement.value.get("results") or []
    for idx, leg_state in enumerate(immediate_legs):
        leg_result = results[idx] if idx < len(results) else None
        if leg_result is None:
            logger.error(
                "%s entry leg %s failed: order response has no result entry; "
                "retaining other leg data.",
                config.strategy_name,
                leg_state.config.offset,
            )
            leg_state.is_open = False
            continue
        _capture_leg_fill(engine_state, leg_state, leg_result, spot=spot, sleep=sleep)

    return True


# ---------------------------------------------------------------------------
# Execution-mode router (Req 16)
# ---------------------------------------------------------------------------
#
# The router applies the configured Execution_Mode to the OpenAlgo platform by
# toggling its analyzer state via the SDK *before* any orders are placed, then
# reads the platform's authoritative analyzer status to determine the
# **effective** mode that actually governs routing (Req 16.6). Sandbox mode maps
# to analyzer/analyze mode = on, so every order is routed through the Analyzer
# and never to the live broker (Req 16.2); live mode maps to analyze mode = off
# so orders route to the broker, but only when explicitly configured (Req 16.4).
# Because the platform's effective state is the source of truth, a live
# configuration the platform refuses to honour (e.g. the analyzer is locked on)
# is detected as a mismatch, logged, and the effective mode is followed instead
# (Req 16.6). The active mode is logged for visibility (Req 16.3).


def _extract_analyze_mode(response: Any) -> bool | None:
    """Return the boolean ``data.analyze_mode`` from an analyzer response.

    Accepts the success-shaped envelope returned by ``client.analyzerstatus()``
    and ``client.analyzertoggle()``:
    ``{"status": "success", "data": {"analyze_mode": bool, ...}}``. Returns the
    parsed boolean when present, or ``None`` when the response is not a success
    envelope or carries no boolean ``analyze_mode`` (an indeterminate result the
    caller resolves conservatively).
    """
    if not isinstance(response, dict) or response.get("status") != "success":
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("analyze_mode")
    return value if isinstance(value, bool) else None


def _read_platform_analyze_mode(engine_state: EngineState) -> bool | None:
    """Read the platform's current analyzer state via ``analyzerstatus``.

    Returns the platform's ``analyze_mode`` (``True`` = sandbox/analyze,
    ``False`` = live) or ``None`` when the status cannot be determined (an SDK
    error or malformed response), which the caller treats as indeterminate.
    """
    client = engine_state.client
    try:
        status = client.analyzerstatus()
    except Exception as exc:  # noqa: BLE001 - SDK errors must not crash routing
        logger.error(
            "%s could not read analyzer status: %s",
            engine_state.config.strategy_name,
            exc,
        )
        return None
    return _extract_analyze_mode(status)


def apply_execution_mode(engine_state: EngineState) -> ExecutionMode:
    """Apply the configured Execution_Mode and return the effective mode (Req 16).

    Applies ``config.execution_mode`` to the OpenAlgo platform by toggling its
    analyzer state via ``client.analyzertoggle(mode=...)`` (sandbox -> analyze
    mode on, live -> analyze mode off), then reads ``client.analyzerstatus()`` to
    learn the platform's **effective** analyzer state, which governs actual
    routing (Req 16.6). The resulting active mode is logged (Req 16.3).

    Routing semantics:
        * Sandbox (Req 16.2): analyze mode is turned on so every order is routed
          through the Analyzer/sandbox and never to the live broker.
        * Live (Req 16.4): analyze mode is turned off so orders route to the
          broker, but only when live is explicitly configured *and* the platform
          honours the switch.
        * Mismatch (Req 16.6): when live is configured but the platform's
          effective analyzer state still prevents live routing (analyze mode
          stays on), the mismatch is logged and the effective (sandbox) mode is
          returned so the caller follows the platform's effective mode.

    If the platform analyzer state cannot be determined at all (an SDK error or
    malformed responses), the effective mode conservatively defaults to sandbox
    so no order is ever unexpectedly routed to the live broker.

    Args:
        engine_state: The engine state; reads ``config`` and ``client``.

    Returns:
        The effective :class:`ExecutionMode` that governs order routing.
    """
    config = engine_state.config
    client = engine_state.client
    desired = config.execution_mode
    desired_analyze = desired is ExecutionMode.SANDBOX

    # Apply the desired mode by toggling the platform analyzer state (design
    # section 10): sandbox -> analyze on, live -> analyze off.
    try:
        client.analyzertoggle(mode=desired_analyze)
    except Exception as exc:  # noqa: BLE001 - SDK errors must not crash routing
        logger.error(
            "%s analyzer toggle to %s mode raised: %s",
            config.strategy_name,
            desired.value,
            exc,
        )

    # The platform's effective analyzer state is authoritative for routing.
    effective_analyze = _read_platform_analyze_mode(engine_state)
    if effective_analyze is None:
        logger.error(
            "%s could not determine platform analyzer state; defaulting to "
            "sandbox routing for safety.",
            config.strategy_name,
        )
        effective_analyze = True

    effective_mode = (
        ExecutionMode.SANDBOX if effective_analyze else ExecutionMode.LIVE
    )

    if effective_mode is not desired:
        logger.warning(
            "%s execution-mode mismatch: configured %s but platform effective "
            "mode is %s; routing orders according to the platform's effective "
            "mode.",
            config.strategy_name,
            desired.value,
            effective_mode.value,
        )

    logger.info(
        "%s active execution mode: %s (orders route %s).",
        config.strategy_name,
        effective_mode.value,
        "through the Analyzer/sandbox"
        if effective_mode is ExecutionMode.SANDBOX
        else "to the live broker",
    )
    return effective_mode


# ---------------------------------------------------------------------------
# Monitoring loop (Req 14, 13.6)
# ---------------------------------------------------------------------------
#
# The monitoring loop is the heartbeat of a live strategy: while any leg is
# open it fetches the current LTP for every monitored instrument, advances the
# per-leg and overall trailing ratchets, and asks the precedence dispatcher
# (:func:`evaluate_risk_cycle`) which single risk action — if any — fires this
# cycle (Req 14.1, 14.7). It follows a "fail safe, keep monitoring" posture:
#
#   * A failed LTP fetch is logged, the last-known LTP and open-leg state are
#     retained, and the loop continues on the next cycle without exiting
#     (Req 14.6).
#   * When any open leg has no known LTP, the aggregate-MTM (overall) checks are
#     skipped for that cycle and a stale-data error is surfaced, while the
#     per-leg checks still run against whatever prices are known (Req 13.6).
#
# LTP delivery has two interchangeable transports that feed the *same* per-cycle
# evaluation (:func:`_advance_and_evaluate`):
#
#   * **Polling** (default): :func:`client.quotes` is called for each monitored
#     instrument at the configured interval (default 1.0s, Req 14.2).
#   * **WebSocket** (optional): the loop subscribes over ``ws_url`` and evaluates
#     on each received tick; if the connection cannot be established or drops
#     while legs are open, it logs and falls back to polling without exiting
#     (Req 14.4, 14.5).
#
# The loop is deliberately injectable so tests never run forever: ``sleep`` and
# ``should_continue`` are parameters, the polling ``fetch`` and the ``ws_runner``
# transport can be swapped for in-memory fakes, and the risk-action ``handler``
# is a callback (defaulting to a logger) rather than hard-wired to the entry /
# re-entry / exit machinery, which the engine orchestration wires in later.


def _extract_quote_ltp(response: Any) -> float | None:
    """Return the numeric LTP from a ``client.quotes()`` response, or ``None``.

    Accepts the success-shaped SDK envelope
    ``{"status": "success", "data": {"ltp": <number>}}`` and returns the LTP as
    a ``float``. Returns ``None`` when the response is not a success envelope,
    carries no ``data`` mapping, or the ``ltp`` is missing or non-numeric
    (``bool`` is rejected). A ``None`` result signals an unusable quote so the
    caller retains the last-known price (Req 14.6).
    """
    if not isinstance(response, dict) or response.get("status") != "success":
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    ltp = data.get("ltp")
    if isinstance(ltp, bool) or not isinstance(ltp, (int, float)):
        return None
    return float(ltp)


def fetch_quote_ltp(client: object, symbol: str, exchange: str) -> float | None:
    """Fetch a single instrument's LTP via ``client.quotes()`` (Req 14.1, 14.2).

    Wraps the SDK call so that any SDK error or malformed response resolves to
    ``None`` rather than propagating: the loop treats ``None`` as a fetch
    failure and retains the last-known price (Req 14.6). This is the default
    polling fetcher; tests inject their own in-memory fetcher instead.

    Args:
        client: The OpenAlgo SDK client (or a fake exposing ``quotes``).
        symbol: The instrument trading symbol.
        exchange: The exchange to quote on (index exchange for the spot, F&O
            exchange for option legs).

    Returns:
        The instrument LTP as a ``float``, or ``None`` on any failure.
    """
    try:
        response = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:  # noqa: BLE001 - SDK errors must not kill the loop
        logger.error("LTP fetch for %s:%s raised: %s", exchange, symbol, exc)
        return None
    return _extract_quote_ltp(response)


#: Type of the pluggable polling fetcher: ``fetch(symbol, exchange) -> ltp``.
LtpFetcher = Callable[[str, str], "float | None"]

#: Type of the risk-decision handler invoked once per fired decision.
RiskHandler = Callable[[EngineState, RiskDecision], None]


def _make_quote_fetch(client: object) -> LtpFetcher:
    """Return a polling fetcher bound to ``client`` (see :func:`fetch_quote_ltp`)."""
    return lambda symbol, exchange: fetch_quote_ltp(client, symbol, exchange)


def _refresh_spot(engine_state: EngineState, fetch: LtpFetcher) -> float | None:
    """Refresh and return the underlying spot, retaining the last value on failure.

    Fetches the index spot on its index exchange (e.g. ``NSE_INDEX`` for NIFTY).
    On success the value is stored in ``engine_state.last_spot`` and returned; on
    failure the error is logged and the previously stored ``last_spot`` is
    returned unchanged (Req 14.6).
    """
    index = engine_state.config.index
    ltp = fetch(index.name, index.index_exchange)
    if ltp is None:
        logger.error(
            "%s spot LTP fetch failed for %s:%s; retaining last-known spot %r.",
            engine_state.config.strategy_name,
            index.index_exchange,
            index.name,
            engine_state.last_spot,
        )
        return engine_state.last_spot
    engine_state.last_spot = ltp
    return ltp


def _refresh_leg_ltp(engine_state: EngineState, leg: LegState, fetch: LtpFetcher) -> None:
    """Refresh one open leg's LTP in place, retaining the last value on failure.

    On a successful fetch ``leg.last_ltp`` is updated; on failure the error is
    logged and ``leg.last_ltp`` is left unchanged so the leg keeps its
    last-known price and open state (Req 14.6). Legs with no recorded symbol are
    skipped (nothing to quote).
    """
    if not leg.symbol:
        return
    ltp = fetch(leg.symbol, engine_state.config.index.fno_exchange)
    if ltp is None:
        logger.error(
            "%s LTP fetch failed for %s:%s; retaining last-known LTP %r.",
            engine_state.config.strategy_name,
            engine_state.config.index.fno_exchange,
            leg.symbol,
            leg.last_ltp,
        )
        return
    leg.last_ltp = ltp


def _poll_refresh(engine_state: EngineState, fetch: LtpFetcher) -> float | None:
    """Fetch the spot and every open leg's LTP for one polling cycle (Req 14.1).

    Returns the (possibly retained) spot for forwarding to the per-leg
    ``UnderlyingPoints`` evaluator. Each instrument is refreshed independently so
    one failed fetch never discards another leg's fresh price (Req 14.6).
    """
    spot = _refresh_spot(engine_state, fetch)
    for leg in engine_state.legs:
        if leg.is_open:
            _refresh_leg_ltp(engine_state, leg, fetch)
    return spot


def _has_open_legs(engine_state: EngineState) -> bool:
    """Return whether any leg (open position or pending momentum entry) is live.

    The loop runs while there is still work to do: an open leg to risk-manage or
    a deferred momentum leg still awaiting its entry trigger (Req 14.1). Once
    neither remains the loop can stop (Req 15.4 handles the closed-out case).
    """
    return any(leg.is_open or leg.pending_momentum for leg in engine_state.legs)


def _has_stale_open_leg(engine_state: EngineState) -> bool:
    """Return whether any open leg lacks a known LTP (stale data, Req 13.6).

    Only *open* legs are considered; a deferred momentum leg is not yet a
    position and does not gate the aggregate-MTM evaluation.
    """
    return any(leg.is_open and leg.last_ltp is None for leg in engine_state.legs)


def _advance_and_evaluate(
    engine_state: EngineState,
    spot: float | None = None,
) -> RiskDecision | None:
    """Advance the ratchets and run the precedence dispatcher for one cycle.

    This is the shared per-cycle evaluation used by both transports once the
    current LTPs are in ``engine_state`` (polling stores them via
    :func:`_poll_refresh`; the WebSocket path stores them from each tick). It:

        1. Advances every open leg's trailing-stop ratchet
           (:func:`update_leg_trail`) so the trail reflects the latest premium
           before it is tested (Req 11).
        2. Checks for stale data: if any open leg has no known LTP, it logs a
           stale-data error, skips the overall (aggregate-MTM) checks and the
           overall-trail ratchet advance this cycle, and evaluates only the
           per-leg conditions (Req 13.6).
        3. Otherwise computes the aggregate MTM, advances the overall-trail lock
           (:func:`update_overall_trail`), and evaluates all conditions.

    The dispatcher (:func:`evaluate_risk_cycle`) returns the single
    highest-precedence :class:`RiskDecision`, or ``None`` when nothing fired.
    Acting on the decision is the caller's responsibility.

    Args:
        engine_state: The engine state carrying the current LTPs and ratchets.
        spot: The current underlying spot for ``UnderlyingPoints`` leg stops;
            defaults to ``engine_state.last_spot`` when not supplied.

    Returns:
        The fired :class:`RiskDecision`, or ``None``.
    """
    if spot is None:
        spot = engine_state.last_spot

    # Advance leg trailing ratchets before they are tested (Req 11.2).
    for leg in engine_state.legs:
        if leg.is_open:
            update_leg_trail(leg)

    # Stale data: skip aggregate MTM this cycle, keep per-leg checks (Req 13.6).
    if _has_stale_open_leg(engine_state):
        logger.error(
            "%s stale/missing market data: an open leg has no known LTP; "
            "skipping aggregate MTM evaluation this cycle.",
            engine_state.config.strategy_name,
        )
        return evaluate_risk_cycle(
            engine_state, spot=spot, evaluate_overall=False
        )

    # Fresh data: advance the overall-trail lock, then evaluate everything.
    mtm = aggregate_mtm(engine_state)
    update_overall_trail(engine_state, mtm)
    return evaluate_risk_cycle(
        engine_state, spot=spot, mtm=mtm, evaluate_overall=True
    )


def log_risk_decision(engine_state: EngineState, decision: RiskDecision) -> None:
    """Default risk-decision handler: log the fired action and its context.

    Used when :func:`monitor` is called without an explicit ``handler``. It logs
    which precedence condition fired and the leg/MTM context so the surfaced
    decision is visible; the entry / re-entry / exit machinery is wired in by the
    engine orchestration, which passes its own handler.
    """
    if decision.leg_state is not None:
        leg = decision.leg_state
        logger.info(
            "%s risk decision %s for leg %s %s (last_ltp=%r).",
            engine_state.config.strategy_name,
            decision.action.value,
            OptionType(leg.config.option_type).value,
            leg.config.offset,
            leg.last_ltp,
        )
    else:
        logger.info(
            "%s risk decision %s (mtm=%r).",
            engine_state.config.strategy_name,
            decision.action.value,
            decision.mtm,
        )


def _ws_instruments(engine_state: EngineState) -> list[dict[str, str]]:
    """Build the WebSocket subscription list: index spot plus every leg symbol.

    Returns instrument dicts in the SDK's ``{"exchange": ..., "symbol": ...}``
    shape (Req 14.4). The index spot is included so ``UnderlyingPoints`` legs and
    the ATM reference stay current alongside the option legs.
    """
    index = engine_state.config.index
    instruments: list[dict[str, str]] = [
        {"exchange": index.index_exchange, "symbol": index.name}
    ]
    for leg in engine_state.legs:
        if leg.symbol:
            instruments.append(
                {"exchange": index.fno_exchange, "symbol": leg.symbol}
            )
    return instruments


def _apply_ws_tick(engine_state: EngineState, update: Any) -> bool:
    """Apply one WebSocket LTP tick to the engine state; return whether it matched.

    Accepts the SDK LTP callback payload
    ``{"symbol": ..., "exchange": ..., "data": {"ltp": <number>}}`` and stores the
    LTP on the matching leg(s) or the index spot. Ticks that are malformed,
    carry a non-numeric LTP, or reference an unknown instrument are ignored
    (returning ``False``) so a stray tick never corrupts state.
    """
    if not isinstance(update, dict):
        return False
    symbol = update.get("symbol")
    data = update.get("data")
    if not isinstance(data, dict):
        return False
    ltp = data.get("ltp")
    if isinstance(ltp, bool) or not isinstance(ltp, (int, float)):
        return False
    ltp = float(ltp)

    index = engine_state.config.index
    matched = False
    if symbol == index.name:
        engine_state.last_spot = ltp
        matched = True
    for leg in engine_state.legs:
        if leg.symbol is not None and leg.symbol == symbol:
            leg.last_ltp = ltp
            matched = True
    return matched


def _default_ws_runner(
    engine_state: EngineState,
    *,
    handler: RiskHandler,
    should_continue: Callable[[], bool],
    sleep: Callable[[float], None],
    interval: float,
) -> bool:
    """Run WebSocket monitoring via the SDK; return ``True`` only if it completes.

    Attempts to ``connect()`` and ``subscribe_ltp()`` over the client's
    ``ws_url`` with a tick handler that stores each LTP and drives one
    :func:`_advance_and_evaluate` cycle, passing any fired decision to
    ``handler`` (Req 14.4). The main thread parks between ``sleep`` intervals,
    re-checking ``should_continue`` and whether any leg is still live.

    Returns ``True`` only when monitoring ran to a clean finish (no legs remain
    or the caller asked to stop). Any inability to establish the connection, a
    ``connect``/``subscribe`` returning falsey, a dropped connection
    (``client.connected`` going false while legs are open), or a missing WS API
    on the client returns ``False`` so :func:`monitor` falls back to polling
    without exiting (Req 14.5).
    """
    client = engine_state.client
    connect = getattr(client, "connect", None)
    subscribe = getattr(client, "subscribe_ltp", None)
    if not callable(connect) or not callable(subscribe):
        logger.error(
            "%s WebSocket monitoring requested but the client exposes no "
            "connect/subscribe API; falling back to polling.",
            engine_state.config.strategy_name,
        )
        return False

    def _on_tick(update: Any) -> None:
        if not _apply_ws_tick(engine_state, update):
            return
        decision = _advance_and_evaluate(engine_state)
        if decision is not None:
            handler(engine_state, decision)

    try:
        if not connect():
            logger.error(
                "%s WebSocket connect failed; falling back to polling.",
                engine_state.config.strategy_name,
            )
            return False
        if not subscribe(_ws_instruments(engine_state), on_data_received=_on_tick):
            logger.error(
                "%s WebSocket subscribe failed; falling back to polling.",
                engine_state.config.strategy_name,
            )
            return False

        # Park the main thread while ticks arrive on the SDK's feed thread.
        while engine_state.running and _has_open_legs(engine_state):
            if not should_continue():
                break
            if getattr(client, "connected", True) is False:
                logger.error(
                    "%s WebSocket connection dropped while legs open; falling "
                    "back to polling.",
                    engine_state.config.strategy_name,
                )
                return False
            sleep(interval)
    except Exception as exc:  # noqa: BLE001 - any WS error must fall back, not crash
        logger.error(
            "%s WebSocket monitoring error: %s; falling back to polling.",
            engine_state.config.strategy_name,
            exc,
        )
        return False
    finally:
        _safe_ws_teardown(engine_state)

    return True


def _safe_ws_teardown(engine_state: EngineState) -> None:
    """Best-effort WebSocket unsubscribe/disconnect that never raises."""
    client = engine_state.client
    for method in ("unsubscribe_ltp", "disconnect"):
        fn = getattr(client, method, None)
        if not callable(fn):
            continue
        try:
            if method == "unsubscribe_ltp":
                fn(_ws_instruments(engine_state))
            else:
                fn()
        except Exception as exc:  # noqa: BLE001 - teardown must be silent
            logger.debug(
                "%s WebSocket %s during teardown raised: %s",
                engine_state.config.strategy_name,
                method,
                exc,
            )


def _poll_loop(
    engine_state: EngineState,
    *,
    sleep: Callable[[float], None],
    should_continue: Callable[[], bool],
    handler: RiskHandler,
    fetch: LtpFetcher,
    interval: float,
) -> None:
    """Run the fixed-cadence polling loop while any leg is live (Req 14.1, 14.2).

    Each cycle fetches the spot and every open leg's LTP (retaining last-known
    values on failure, Req 14.6), advances the ratchets, evaluates the
    precedence dispatcher via :func:`_advance_and_evaluate`, dispatches any fired
    decision to ``handler``, and then sleeps for ``interval`` seconds. The loop
    exits when the engine stops running, no legs remain live, or
    ``should_continue`` returns ``False`` (the injectable stop condition).
    """
    while engine_state.running and _has_open_legs(engine_state):
        if not should_continue():
            break
        spot = _poll_refresh(engine_state, fetch)
        decision = _advance_and_evaluate(engine_state, spot=spot)
        if decision is not None:
            handler(engine_state, decision)
        if not (engine_state.running and _has_open_legs(engine_state)):
            break
        sleep(interval)


def monitor(
    engine_state: EngineState,
    *,
    sleep: Callable[[float], None] = _sleep,
    should_continue: Callable[[], bool] | None = None,
    handler: RiskHandler | None = None,
    fetch: LtpFetcher | None = None,
    ws_runner: Callable[..., bool] | None = None,
) -> None:
    """Run the fixed-cadence risk loop while any leg is open (Req 14).

    This is the live monitoring heartbeat. While any leg is open (or a momentum
    leg is still pending), it fetches the LTP for every monitored instrument,
    advances the leg and overall trailing ratchets, and invokes the precedence
    dispatcher (:func:`evaluate_risk_cycle`) to select the single
    highest-precedence risk action for the cycle, handing any fired decision to
    ``handler`` (Req 14.1, 14.7).

    Transport selection:
        * When ``config.use_websocket`` is set, WebSocket monitoring is attempted
          first via ``ws_runner`` (default :func:`_default_ws_runner`): it
          subscribes over ``ws_url`` and evaluates on each tick (Req 14.4). If it
          cannot establish the connection or the connection drops while legs are
          open, it logs and this function falls back to polling without exiting
          (Req 14.5). A clean WebSocket completion (all legs closed) returns
          without polling.
        * Otherwise (or after a WebSocket fallback) it polls
          :func:`client.quotes` every ``monitoring_interval`` seconds (default
          1.0s; invalid values are coerced to the default per Req 14.2/14.3).

    Resilience: a failed LTP fetch is logged and the last-known LTP and leg
    state are retained (Req 14.6); when any open leg lacks a known LTP the
    aggregate-MTM checks are skipped for that cycle with a stale-data error while
    per-leg checks still run (Req 13.6).

    Testability: ``sleep`` and ``should_continue`` let tests bound the loop so it
    never runs forever, ``fetch`` swaps the polling transport for an in-memory
    fetcher, ``ws_runner`` swaps the WebSocket transport, and ``handler``
    receives each decision (defaulting to :func:`log_risk_decision`).

    Args:
        engine_state: The engine state carrying config, client, legs, and
            ratchets. The loop reads and mutates leg ``last_ltp``, the ratchets,
            and ``last_spot``.
        sleep: Sleep callable used between cycles; injectable so tests avoid real
            delays. Defaults to :func:`time.sleep`.
        should_continue: Optional zero-argument predicate consulted at the top of
            each cycle; returning ``False`` stops the loop. Defaults to always
            continuing (the loop then stops only when no legs remain live).
        handler: Callback invoked with ``(engine_state, decision)`` for each
            fired risk decision. Defaults to :func:`log_risk_decision`.
        fetch: Optional polling fetcher ``fetch(symbol, exchange) -> ltp``;
            defaults to a :func:`client.quotes`-backed fetcher.
        ws_runner: Optional WebSocket transport; defaults to
            :func:`_default_ws_runner`. Only used when ``config.use_websocket``.
    """
    handler = handler or log_risk_decision
    _should = should_continue or (lambda: True)
    interval = _coerce_monitoring_interval(engine_state.config.monitoring_interval)

    if engine_state.config.use_websocket:
        runner = ws_runner or _default_ws_runner
        try:
            completed = runner(
                engine_state,
                handler=handler,
                should_continue=_should,
                sleep=sleep,
                interval=interval,
            )
        except Exception as exc:  # noqa: BLE001 - WS failures fall back, not crash
            logger.error(
                "%s WebSocket monitoring raised %s; falling back to polling.",
                engine_state.config.strategy_name,
                exc,
            )
            completed = False
        if completed:
            return
        logger.info(
            "%s falling back to polling LTP via quotes() at %.1fs interval.",
            engine_state.config.strategy_name,
            interval,
        )

    poll_fetch = fetch or _make_quote_fetch(engine_state.client)
    _poll_loop(
        engine_state,
        sleep=sleep,
        should_continue=_should,
        handler=handler,
        fetch=poll_fetch,
        interval=interval,
    )


# ---------------------------------------------------------------------------
# Exit manager (Req 13.5, 15)
# ---------------------------------------------------------------------------
#
# The exit manager is the single close-out path for a running strategy. It
# turns the abstract "close this leg" / "close everything" intents into
# square-off orders on the SDK, realizes each closed leg's MTM into
# ``EngineState.realized_mtm``, and decides when the strategy is flat so the
# monitoring loop can stop and log a final summary. It handles three triggers:
#
#   * **Overall SL / trail** (Req 15.1, 15.2): when ``square_off_all_legs`` is
#     true, every open leg is squared off in the same cycle; when false, only
#     the legs designated for the trigger are closed and the rest are left
#     untouched.
#   * **Scheduled exit** (Req 15.3): at system time >= Exit_Time every open leg
#     is squared off regardless of ``square_off_all_legs`` or any overall state,
#     and any never-triggered momentum leg is abandoned (Req 9.5) so the loop
#     can wind down.
#   * **Completion** (Req 15.4, 15.5): once nothing is live the loop is stopped
#     and the realized-MTM summary is logged.
#
# Every square-off reuses the shared bounded-retry policy (:func:`run_with_retry`,
# Req 10.6): up to three attempts in the same cycle. A leg that still cannot be
# closed is left open with its state intact and an error is surfaced naming it;
# the caller re-invokes the exit manager on each subsequent cycle so the leg is
# retried until it closes or the process terminates (Req 13.5, 15.6). Successful
# closes are never disturbed by a later failed retry of a sibling leg.


def _closing_action(action: Action | str) -> Action:
    """Return the order side that flattens a position opened with ``action``.

    A short (``SELL``) leg is closed by buying it back; a long (``BUY``) leg is
    closed by selling it. This is a pure helper.

    Raises:
        ValueError: If ``action`` is neither BUY nor SELL.
    """
    opened = Action(action)
    if opened is Action.SELL:
        return Action.BUY
    if opened is Action.BUY:
        return Action.SELL
    raise ValueError(f"leg action {action!r} is neither BUY nor SELL")


def _squareoff_succeeded(response: Any) -> bool:
    """Return whether a square-off order response reports success.

    Accepts the success-shaped ``placeorder`` envelope
    ``{"status": "success", "orderid": ...}``; anything else (an error status or
    a non-dict) counts as a failed attempt so :func:`run_with_retry` retries it.
    """
    return isinstance(response, dict) and response.get("status") == "success"


def _place_square_off_order(
    engine_state: EngineState,
    leg_state: LegState,
    *,
    sleep: Callable[[float], None] = _sleep,
) -> RetryResult:
    """Submit a bounded-retry MARKET square-off for one open leg (Req 10.6).

    Places a ``MARKET`` order on the index F&O exchange for the leg's recorded
    option ``symbol`` using the flattening side (:func:`_closing_action`) and the
    full strategy quantity, wrapped in :func:`run_with_retry` so it is attempted
    at most :data:`MAX_RETRY_ATTEMPTS` times and stops on the first success. The
    helper only submits the order; it never mutates leg or engine state, leaving
    that to :func:`square_off_leg`.

    Args:
        engine_state: The engine state (client, strategy name, quantity, product,
            and index exchange).
        leg_state: The open leg to close (supplies ``symbol`` and entry action).
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        The :class:`RetryResult` describing whether the square-off order was
        accepted, and how many attempts were made.
    """
    config = engine_state.config
    client = engine_state.client
    close_action = _closing_action(leg_state.config.action)
    return run_with_retry(
        lambda: client.placeorder(
            strategy=config.strategy_name,
            symbol=leg_state.symbol,
            action=close_action.value,
            exchange=config.index.fno_exchange,
            price_type="MARKET",
            product=config.product,
            quantity=str(config.quantity),
            price="0",
        ),
        is_success=_squareoff_succeeded,
        sleep=sleep,
        description=f"{config.strategy_name} square-off {leg_state.symbol}",
    )


def square_off_leg(
    engine_state: EngineState,
    leg_state: LegState,
    *,
    reason: str,
    sleep: Callable[[float], None] = _sleep,
) -> bool:
    """Close a single open leg, realizing its MTM on success (Req 15.5, 15.6).

    This is the canonical per-leg close path shared by every trigger (overall
    SL/trail, scheduled exit, and — when the orchestration wires it — leg-level
    SL/trail). A leg that is already closed is a no-op success. Otherwise a
    bounded-retry square-off order is submitted (:func:`_place_square_off_order`,
    Req 10.6):

        * **On success** the leg's mark-to-market P&L at its last-known LTP is
          added to ``engine_state.realized_mtm`` (skipped only when the MTM is
          undefined for lack of a fill or price), the leg is marked closed
          (``is_open = False``), its ``exit_reason`` is recorded, and its
          ``order_id`` is updated to the closing order when the response carries
          one. The close is logged with the leg, side, quantity, and price
          context (Req 18.1).
        * **On failure** the leg is left fully intact (still open, MTM not
          realized) and an error naming the affected leg is surfaced; the caller
          retries it on the next cycle (Req 15.6).

    Args:
        engine_state: The engine state (client, config, realized-MTM tally).
        leg_state: The leg to close.
        reason: A short label for why the leg is being closed (e.g.
            ``"overall_stop_loss"``, ``"scheduled_exit"``), recorded as the
            leg's ``exit_reason``.
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        ``True`` when the leg is confirmed closed (including an already-closed
        leg), otherwise ``False``.
    """
    if not leg_state.is_open:
        return True

    result = _place_square_off_order(engine_state, leg_state, sleep=sleep)
    if not result.succeeded:
        logger.error(
            "%s square-off failed for leg %s after %d attempt(s); leg remains "
            "open and will be retried next cycle.",
            engine_state.config.strategy_name,
            leg_state.symbol or leg_state.config.offset,
            result.attempts,
        )
        return False

    realized = leg_mtm(leg_state, engine_state.config.quantity)
    if realized is not None:
        engine_state.realized_mtm += realized

    leg_state.is_open = False
    leg_state.exit_reason = reason
    if isinstance(result.value, dict) and result.value.get("orderid") is not None:
        leg_state.order_id = result.value.get("orderid")

    logger.info(
        "%s squared off leg %s action=%s qty=%d ltp=%r realized=%r reason=%s",
        engine_state.config.strategy_name,
        leg_state.symbol or leg_state.config.offset,
        _closing_action(leg_state.config.action).value,
        engine_state.config.quantity,
        leg_state.last_ltp,
        realized,
        reason,
    )
    return True


def square_off_legs(
    engine_state: EngineState,
    legs: list[LegState],
    *,
    reason: str,
    sleep: Callable[[float], None] = _sleep,
) -> list[LegState]:
    """Square off the given legs, returning those still open after retries.

    Each open leg in ``legs`` is closed via :func:`square_off_leg`; legs already
    closed or not part of the engine are skipped. Because each leg is submitted
    independently, one leg's square-off failure never discards another leg's
    successful close: the remaining-open list captures exactly the legs that
    could not be confirmed closed this cycle so the caller can retry them (and
    only them) next cycle and report which stay open (Req 13.5, 15.6).

    Args:
        engine_state: The engine state.
        legs: The specific legs to square off (the "designated" legs for a
            trigger, per Req 15.2).
        reason: The close reason recorded on each leg (see :func:`square_off_leg`).
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        The list of legs that remain open after this cycle's attempts, in the
        order supplied; empty when every requested leg is confirmed closed.
    """
    still_open: list[LegState] = []
    for leg in legs:
        if not leg.is_open:
            continue
        if not square_off_leg(engine_state, leg, reason=reason, sleep=sleep):
            still_open.append(leg)
    if still_open:
        logger.error(
            "%s square-off incomplete: %d leg(s) remain open (%s); will retry.",
            engine_state.config.strategy_name,
            len(still_open),
            ", ".join(leg.symbol or leg.config.offset for leg in still_open),
        )
    return still_open


def square_off_all_open(
    engine_state: EngineState,
    *,
    reason: str,
    sleep: Callable[[float], None] = _sleep,
) -> list[LegState]:
    """Square off every currently open leg, returning those still open.

    Closes all open legs of the strategy in configured order (Req 15.1, 15.3).
    Delegates to :func:`square_off_legs` so partial failures are retried per leg
    and surfaced by the returned remaining-open list.

    Args:
        engine_state: The engine state.
        reason: The close reason recorded on each leg.
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        The list of legs that remain open after this cycle's attempts; empty when
        every open leg is confirmed closed.
    """
    open_legs = [leg for leg in engine_state.legs if leg.is_open]
    return square_off_legs(engine_state, open_legs, reason=reason, sleep=sleep)


def perform_overall_square_off(
    engine_state: EngineState,
    *,
    designated: list[LegState] | None = None,
    reason: str = "overall",
    sleep: Callable[[float], None] = _sleep,
) -> list[LegState]:
    """Square off on an overall SL/trail trigger, scoped by config (Req 15.1, 15.2).

    Scope selection:
        * ``square_off_all_legs`` **true** — every open leg is squared off in the
          same cycle (Req 15.1); ``designated`` is ignored.
        * ``square_off_all_legs`` **false** — only the ``designated`` legs for the
          trigger are squared off and all other open legs are left unchanged
          (Req 15.2). ``designated`` defaults to an empty list, meaning nothing
          is closed unless the caller names the legs the trigger designates.

    Partial failures are retried per leg and reported via the returned
    remaining-open list (Req 13.5).

    Args:
        engine_state: The engine state; reads ``config.square_off_all_legs``.
        designated: The legs designated for the trigger, used only when
            ``square_off_all_legs`` is false. Defaults to ``None`` (treated as no
            designated legs).
        reason: The close reason recorded on each closed leg.
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        The list of legs that remain open after this cycle's attempts.
    """
    if engine_state.config.square_off_all_legs:
        return square_off_all_open(engine_state, reason=reason, sleep=sleep)
    return square_off_legs(
        engine_state, designated or [], reason=reason, sleep=sleep
    )


def _exit_time_of_day(config: StrategyConfig) -> time:
    """Return the configured ``Exit_Time`` as a :class:`datetime.time` (IST)."""
    return _parse_hms(config.exit_time, "exit_time").time()


def is_at_or_after_exit_time(
    config: StrategyConfig,
    now: time | datetime | None = None,
) -> bool:
    """Return whether the current IST time has reached ``Exit_Time`` (Req 15.3).

    The scheduled exit fires at or after the configured ``Exit_Time`` (the
    boundary itself counts). This is a pure predicate that mutates nothing.

    Args:
        config: The strategy configuration (reads ``exit_time``).
        now: The current IST time, as a :class:`datetime.time` or
            :class:`datetime.datetime` (its time-of-day is used). Defaults to the
            current IST wall-clock time; injectable so tests can pin the boundary.

    Returns:
        ``True`` when ``now`` is at or after ``Exit_Time``, otherwise ``False``.
    """
    exit_time = _exit_time_of_day(config)
    if now is None:
        now_time = datetime.now(_IST).time()
    elif isinstance(now, datetime):
        now_time = now.time()
    else:
        now_time = now
    return now_time >= exit_time


def perform_scheduled_exit(
    engine_state: EngineState,
    *,
    reason: str = "scheduled_exit",
    sleep: Callable[[float], None] = _sleep,
) -> list[LegState]:
    """Force-close the whole strategy at Exit_Time (Req 15.3, Req 9.5).

    Squares off every open leg regardless of ``square_off_all_legs`` or any
    overall stop-loss / trailing state, and abandons any momentum leg still
    awaiting its entry trigger (it is never entered, Req 9.5) so the monitoring
    loop can wind down (Req 15.4). Partial square-off failures are retried per
    leg and reported via the returned remaining-open list, so the caller
    re-invokes this on each subsequent cycle until every leg is closed (Req 15.6).

    Args:
        engine_state: The engine state.
        reason: The close reason recorded on each closed leg.
        sleep: Sleep callable forwarded to the retry policy; injectable for tests.

    Returns:
        The list of legs that remain open after this cycle's attempts; empty when
        every open leg is confirmed closed.
    """
    for leg in engine_state.legs:
        if leg.pending_momentum and not leg.is_open:
            leg.pending_momentum = False
            logger.info(
                "%s momentum leg %s never triggered by Exit_Time; abandoning "
                "(never entered).",
                engine_state.config.strategy_name,
                leg.symbol or leg.config.offset,
            )
    return square_off_all_open(engine_state, reason=reason, sleep=sleep)


def all_legs_closed(engine_state: EngineState) -> bool:
    """Return whether the strategy is flat with nothing left to monitor (Req 15.4).

    The strategy is considered fully closed when no leg is open and no momentum
    leg is still pending entry — the same "is anything live?" test the
    monitoring loop uses to decide whether to keep running (mirrors
    :func:`_has_open_legs`). This is a pure predicate.
    """
    return not _has_open_legs(engine_state)


def log_final_mtm_summary(engine_state: EngineState) -> None:
    """Log the strategy's final realized-MTM summary (Req 15.5).

    Emits a single summary line carrying ``engine_state.realized_mtm`` — the sum
    of every leg's MTM realized as it was squared off. Intended to be called once
    when the monitoring loop stops.
    """
    logger.info(
        "%s final MTM summary: realized_mtm=%.2f",
        engine_state.config.strategy_name,
        engine_state.realized_mtm,
    )


def finalize_exit_if_flat(engine_state: EngineState) -> bool:
    """Stop the loop and log the final summary once the strategy is flat.

    When :func:`all_legs_closed` holds (no open legs and no pending momentum
    leg), this stops the monitoring loop by clearing ``engine_state.running``
    (Req 15.4) and logs the final realized-MTM summary (Req 15.5), returning
    ``True``. When something is still live it makes no change and returns
    ``False``, so it is safe to call every cycle.

    Args:
        engine_state: The engine state.

    Returns:
        ``True`` when the strategy was finalized this call, otherwise ``False``.
    """
    if not all_legs_closed(engine_state):
        return False
    engine_state.running = False
    log_final_mtm_summary(engine_state)
    return True


# ---------------------------------------------------------------------------
# Logger (design section 11; Req 18)
# ---------------------------------------------------------------------------
#
# All observability flows through the module-level ``bjp_portfolio`` logger.
# :func:`configure_logging` attaches a single stdout handler whose timestamps are
# rendered in IST (Asia/Kolkata) so the OpenAlgo Python Strategy Host captures
# IST-timestamped lines under ``logs/strategies/`` (Req 18.4). :func:`log_event`
# is the structured, single-line front door used for the events Requirement 18
# calls out (place / re-enter / square-off actions, risk-trigger conditions, and
# SDK errors); the pre-existing ``logger.info``/``logger.error`` calls elsewhere
# in this module keep working unchanged and inherit the same IST formatting.

#: Format string for stdout log lines: IST timestamp, level, logger name, message.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Date format for the IST timestamp rendered by :class:`ISTFormatter`.
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S %Z"

#: Marker attribute set on the handler installed by :func:`configure_logging`, so
#: repeated calls stay idempotent instead of stacking duplicate handlers.
_IST_HANDLER_FLAG = "_bjp_ist_handler"


class ISTFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that renders record times in IST (Req 18.1).

    Standard formatters use local (or UTC) time; the BJP portfolio schedules and
    reasons entirely in IST, so every emitted line carries an ``Asia/Kolkata``
    timestamp for auditability against the backtest.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Render ``record``'s creation time as an IST-localized string."""
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        return dt.strftime(datefmt or _LOG_DATEFMT)


def configure_logging(
    *,
    stream: Any = None,
    level: int = logging.INFO,
    force: bool = False,
) -> logging.Logger:
    """Attach an IST-timestamped stdout handler to the ``bjp_portfolio`` logger.

    The handler writes single-line, IST-timestamped records to standard output so
    the Strategy Host can capture them (Req 18.4). The call is idempotent: an
    already-installed BJP handler is reused (its stream/level refreshed) rather
    than duplicated, so wiring this at engine startup and in tests is safe.

    Args:
        stream: Output stream for the handler; defaults to ``sys.stdout`` (bound
            at call time so tests can redirect stdout).
        level: Logging level applied to both the logger and the handler.
        force: When ``True``, remove any existing BJP handler and install a fresh
            one instead of reusing it.

    Returns:
        The configured ``bjp_portfolio`` logger.
    """
    target_stream = stream if stream is not None else sys.stdout

    existing = [
        handler
        for handler in logger.handlers
        if getattr(handler, _IST_HANDLER_FLAG, False)
    ]
    if force:
        for handler in existing:
            logger.removeHandler(handler)
        existing = []

    if existing:
        handler = existing[0]
        handler.setStream(target_stream)
    else:
        handler = logging.StreamHandler(target_stream)
        setattr(handler, _IST_HANDLER_FLAG, True)
        logger.addHandler(handler)

    handler.setFormatter(ISTFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    handler.setLevel(level)
    logger.setLevel(level)
    return logger


def _render_field(value: Any) -> str:
    """Render a single field value as a compact, newline-free token.

    Enums are rendered by their ``.value`` (e.g. ``OptionType.CE`` -> ``CE``) so
    action/option-type context reads cleanly; every other value is stringified.
    Any embedded newlines are collapsed to spaces to preserve the single-line
    guarantee.
    """
    if isinstance(value, Enum):
        rendered = str(value.value)
    else:
        rendered = str(value)
    return rendered.replace("\n", " ").replace("\r", " ")


def log_event(kind: str, **fields: Any) -> str:
    """Emit one structured, single-line, IST-timestamped log record (Req 18).

    This is the front door for the events Requirement 18 requires be logged:

    * place / re-enter / square-off actions, carrying ``symbol``, ``action``,
      ``quantity`` and price context as keyword fields (Req 18.1);
    * leg / trail / overall risk triggers, carrying the condition and the values
      that caused it (Req 18.2);
    * SDK errors, carrying the error message (Req 18.3).

    The record is written through the module ``bjp_portfolio`` logger, so it picks
    up the IST timestamp from the handler installed by :func:`configure_logging`
    and is captured on stdout by the Strategy Host (Req 18.4). The event is
    emitted at ``ERROR`` level when ``kind`` names an error (so SDK failures are
    visible), otherwise at ``INFO``.

    Args:
        kind: A short event kind token (e.g. ``"place"``, ``"square_off"``,
            ``"leg_stop_loss"``, ``"sdk_error"``).
        **fields: Arbitrary contextual key/value pairs appended as
            ``key=value`` tokens in insertion order.

    Returns:
        The formatted single-line message (also useful for testing/reuse).
    """
    parts = [str(kind)]
    parts.extend(f"{key}={_render_field(value)}" for key, value in fields.items())
    message = " ".join(parts)
    level = logging.ERROR if "error" in str(kind).lower() else logging.INFO
    logger.log(level, message)
    return message


# ---------------------------------------------------------------------------
# Engine orchestration (Req 1.3, 1.4, 2.6, 6.1, 16.3)
# ---------------------------------------------------------------------------
#
# ``run(config)`` is the single entry-point every thin strategy script calls
# under ``__main__``. It performs a strict sequence:
#
#   1. Configure IST-timestamped logging to stdout.
#   2. Resolve the platform environment (API key, host, ws_url) — exit non-zero
#      on missing API key (Req 2.5).
#   3. Validate the config — exit non-zero on any invalid field (Req 3.8, 3.9).
#   4. Initialize the OpenAlgo SDK client with the resolved endpoints (Req 2.6).
#   5. Log the strategy name, index, active execution mode, and entry/exit times
#      at startup (Req 1.4, 16.3).
#   6. Schedule the entry cron job at ``Entry_Time`` and the exit cron at
#      ``Exit_Time`` on a ``BackgroundScheduler`` with ``Asia/Kolkata`` timezone
#      (Req 6.1).
#   7. Keep the process alive until exit completes or the process is terminated.
#
# The ``_entry_job`` callback resolves expiry, applies the execution mode, and
# places entries; the ``_exit_job`` callback performs the scheduled exit and
# tears down the scheduler. The monitoring loop is started after entry to run
# risk evaluation while any legs are open.


def _entry_job(
    engine: EngineState,
    scheduler: Any,
) -> None:
    """Cron-triggered entry callback (fires at Entry_Time IST; Req 6.1).

    Resolves the weekly expiry, applies the execution-mode routing, places
    entry orders for all non-momentum legs, and starts the monitoring loop.
    Momentum legs are deferred to the monitoring loop (Req 9.1). On any fatal
    error the engine is stopped and the scheduler is shut down.
    """
    config = engine.config
    try:
        engine.expiry = resolve_weekly_expiry(
            engine.client, config.index.name, config.index.fno_exchange,
        )
        apply_execution_mode(engine)
        place_entry(engine, sleep=_sleep)
        monitor(engine, sleep=_sleep)
    except (ConfigError, ExpiryError) as exc:
        logger.error("%s entry failed: %s", config.strategy_name, exc)
        engine.running = False
    except Exception as exc:  # noqa: BLE001 - must not crash the scheduler
        logger.error("%s entry unexpected error: %s", config.strategy_name, exc)
        engine.running = False
    finally:
        finalize_exit_if_flat(engine)


def _exit_job(
    engine: EngineState,
    scheduler: Any,
) -> None:
    """Cron-triggered exit callback (fires at Exit_Time IST; Req 15.3).

    Performs the scheduled exit (squares off every open leg) and stops the
    monitoring loop by clearing ``engine.running``. The scheduler is shut down
    after the exit is confirmed so the process can terminate cleanly.
    """
    try:
        still_open = perform_scheduled_exit(engine, sleep=_sleep)
        while still_open:
            _sleep(1.0)
            still_open = perform_scheduled_exit(engine, sleep=_sleep)
    except Exception as exc:  # noqa: BLE001 - exit must not crash
        logger.error(
            "%s exit unexpected error: %s", engine.config.strategy_name, exc,
        )
    finally:
        engine.running = False
        finalize_exit_if_flat(engine)
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - teardown
            pass


def run(config: StrategyConfig) -> None:
    """Orchestrate a single portfolio strategy end-to-end (Req 1.3, 1.4).

    This is the entry-point every thin per-strategy script calls under
    ``__main__``. It performs environment resolution, config validation,
    SDK client initialization, startup logging, entry/exit scheduling via
    APScheduler with ``Asia/Kolkata`` timezone, and keeps the process alive
    until exit completes or the process is terminated.

    On missing API key the process exits non-zero (Req 2.5). On invalid config
    the process exits non-zero without scheduling any trades (Req 3.8, 3.9).

    Args:
        config: The fully-specified ``StrategyConfig`` from the strategy script.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    configure_logging()

    # Step 1: resolve environment (Req 2)
    try:
        env = resolve_environment(use_websocket=config.use_websocket)
    except ConfigError as exc:
        logger.error("Environment resolution failed: %s", exc)
        sys.exit(1)

    # Step 2: validate config (Req 3)
    try:
        config = validate_config(config)
    except ConfigError as exc:
        logger.error("Configuration validation failed: %s", exc)
        sys.exit(1)

    # Step 3: initialize the SDK client (Req 2.6)
    try:
        from openalgo import api
        client = api(
            api_key=env.api_key,
            host=env.host,
        )
    except Exception as exc:  # noqa: BLE001 - SDK init must not crash
        logger.error("SDK client initialization failed: %s", exc)
        sys.exit(1)

    # Step 4: startup logging (Req 1.4, 16.3)
    logger.info(
        "Starting %s index=%s mode=%s entry=%s exit=%s lots=%d qty=%d",
        config.strategy_name,
        config.index.name,
        config.execution_mode.value,
        config.entry_time,
        config.exit_time,
        config.lots,
        config.quantity,
    )

    # Step 5: build engine state
    engine = EngineState(config=config, client=client)

    # Step 6: schedule entry and exit cron jobs (Req 6.1)
    entry_t = _parse_hms(config.entry_time, "entry_time").time()
    exit_t = _parse_hms(config.exit_time, "exit_time").time()

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        _entry_job,
        "cron",
        args=[engine, scheduler],
        hour=entry_t.hour,
        minute=entry_t.minute,
        second=entry_t.second,
        id=f"{config.strategy_name}_entry",
    )
    scheduler.add_job(
        _exit_job,
        "cron",
        args=[engine, scheduler],
        hour=exit_t.hour,
        minute=exit_t.minute,
        second=exit_t.second,
        id=f"{config.strategy_name}_exit",
    )
    scheduler.start()

    logger.info(
        "%s scheduler started (tz=Asia/Kolkata); entry=%s exit=%s. "
        "Waiting for trading window...",
        config.strategy_name,
        config.entry_time,
        config.exit_time,
    )

    # Step 7: keep the process alive until the engine stops or is terminated
    try:
        while engine.running:
            _sleep(1.0)
    except (KeyboardInterrupt, SystemExit):
        logger.info("%s received shutdown signal.", config.strategy_name)
    finally:
        engine.running = False
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - teardown
            pass
        logger.info(
            "%s process exiting. realized_mtm=%.2f",
            config.strategy_name,
            engine.realized_mtm,
        )
