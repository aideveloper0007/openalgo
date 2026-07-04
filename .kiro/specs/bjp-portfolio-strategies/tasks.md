# Implementation Plan: BJP Portfolio Strategies

## Overview

Build the shared risk engine library `bjp_core.py` first as a set of pure, testable
functions and dataclasses, each paired with a Hypothesis property-based test that
implements one of the 26 correctness properties from the design. Then assemble the
integration pieces (entry executor, execution-mode router, monitoring loop, exit
manager, logger, and the `run()` orchestration with APScheduler) with mocked-SDK
integration tests. Finally author the 10 thin per-strategy config scripts from the
Per-Strategy Parameter Table and verify their structure and fidelity.

All production code lives in `strategies/bjp_portfolio/` (excluded from Ruff). All
tests live under `test/bjp_portfolio/`, must be lint-clean, and run via
`uv run pytest`. The OpenAlgo SDK client is mocked/faked (in-memory) in every test so
property tests can run 100+ iterations without network cost.

## Tasks

- [x] 1. Set up package structure, data models, and test harness
  - [x] 1.1 Create `strategies/bjp_portfolio/bjp_core.py` with enums, dataclasses, and the index registry
    - Create the `strategies/bjp_portfolio/` directory and `bjp_core.py`
    - Define enums: `OptionType`, `Action`, `SLKind`, `ReentryKind`, `ExecutionMode`
    - Define config dataclasses: `LegStopLoss`, `LegTrailSL`, `LegMomentum`, `LegReentry`, `LegConfig`, `OverallStopLoss`, `OverallTrailSL`, `IndexSpec`, `StrategyConfig` (with the `quantity` property returning `lots * index.lot_size`)
    - Define runtime state dataclasses: `LegState`, `EngineState`
    - Define the `NIFTY` and `SENSEX` `IndexSpec` registry constants (NIFTY: NSE_INDEX/NFO/65/50; SENSEX: BSE_INDEX/BFO/20/100)
    - Define `ConfigError` and `ExpiryError` exception types
    - Create `test/bjp_portfolio/` with an in-memory fake OpenAlgo SDK client fixture and ensure `uv run pytest` discovers it
    - _Requirements: 3.2_

  - [x] 1.2 Write property test for total quantity
    - **Property 2: Total quantity is lots times lot size**
    - **Validates: Requirements 3.2, 7.1**
    - Generate lots in 1–100 and either index; assert `quantity == lots * lot_size` and is a positive integer multiple of the lot size
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 2: ...`

- [x] 2. Implement the environment resolver
  - [x] 2.1 Implement `resolve_environment()` and `ResolvedEnv`
    - Resolve `api_key` from `OPENALGO_API_KEY`; empty → raise `ConfigError` so the caller exits non-zero
    - Resolve REST `host`: first non-empty of `HOST_SERVER`, `OPENALGO_HOST`, else `http://127.0.0.1:5000`
    - Resolve `ws_url`: `WEBSOCKET_URL` if non-empty, else construct `ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}`; if WebSocket enabled and parts incomplete, log the missing var and signal polling fallback without exiting
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 2.2 Write property test for environment precedence
    - **Property 1: Environment resolution follows precedence**
    - **Validates: Requirements 2.2, 2.3**
    - Generate set/empty/unset combinations for the five env vars; assert resolved host and ws endpoint follow the precedence rules
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 1: ...`

  - [x] 2.3 Write edge-case tests for env failures
    - Missing API key raises `ConfigError` (drives non-zero exit) (Req 2.5)
    - WebSocket enabled with incomplete parts logs the missing var and falls back to polling without exiting (Req 2.4)
    - _Requirements: 2.4, 2.5_

- [x] 3. Implement the config validator
  - [x] 3.1 Implement `validate_config()` with env overrides and coercion
    - Apply environment-variable overrides before validation (Req 3.7)
    - Validate `lots` (1–100), `index` (supported pair), `entry_time`/`exit_time` (`HH:MM:SS` and `exit_time > entry_time`), `execution_mode` (sandbox/live), defaulting to sandbox when unconfigured
    - Validate `monitoring_interval`: numeric within 0.1–60s else log error and coerce to 1.0s
    - On any invalid field: log the offending parameter and return a failure that prevents scheduling/trading
    - _Requirements: 3.1, 3.3, 3.4, 3.6, 3.7, 3.8, 3.9, 14.2, 14.3, 16.1, 16.5_

  - [x] 3.2 Write property test for invalid-config rejection
    - **Property 3: Invalid configuration is rejected and never begins trading**
    - **Validates: Requirements 3.1, 3.4, 3.8, 3.9, 16.5**
    - Generate configs with at least one malformed field; assert validation fails, names the offending parameter, and places no entry orders
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 3: ...`

  - [x] 3.3 Write property test for environment overrides
    - **Property 4: Environment overrides take precedence over in-script defaults**
    - **Validates: Requirements 3.7**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 4: ...`

  - [x] 3.4 Write property test for monitoring-interval validation
    - **Property 5: Monitoring interval is validated and defaulted**
    - **Validates: Requirements 3.6, 14.2, 14.3**
    - Include numeric-in-range, out-of-range, and non-numeric inputs; assert coercion to 1.0 default when invalid
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 5: ...`

  - [x] 3.5 Write property test for execution-mode default
    - **Property 26: Execution mode defaults to sandbox**
    - **Validates: Requirements 16.1, 16.5**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 26: ...`

- [x] 4. Implement strike helpers
  - [x] 4.1 Implement `round_atm()`, `apply_offset()`, and the strike-type→offset mapping
    - `round_atm(spot, step)` using `math.floor(spot/step + 0.5)` for half-up rounding (step 50 NIFTY / 100 SENSEX)
    - `apply_offset(atm, step, offset, option_type)`: CE OTM above / ITM below ATM; PE OTM below / ITM above ATM; ATM → atm; one step per unit
    - Strike-type→offset mapping function preserving direction token and integer distance (`OTM<n>`→`OTM<n>`, `ITM<n>`→`ITM<n>`, `ATM`→`ATM`) for n in 0–50
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 4.2 Write property test for strike-type→offset mapping
    - **Property 6: Strike-type to offset mapping preserves token and distance**
    - **Validates: Requirements 4.1**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 6: ...`

  - [x] 4.3 Write property test for offset direction per option type
    - **Property 7: Offset direction is correct per option type**
    - **Validates: Requirements 4.2, 4.3**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 7: ...`

  - [x] 4.4 Write property test for ATM rounding
    - **Property 8: ATM rounding is nearest multiple with ties rounding up**
    - **Validates: Requirements 4.4**
    - Include the exact-half-step boundary (e.g., spot 25 above a 50-step multiple rounds up)
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 8: ...`

- [x] 5. Implement the expiry resolver
  - [x] 5.1 Implement `resolve_weekly_expiry()`
    - Call `client.expiry(symbol, exchange=fno_exchange, instrumenttype="options")` inside a worker thread with `future.result(timeout=10)`
    - Map NIFTY→`NFO`, SENSEX→`BFO`; unsupported index → error and no expiry call
    - Parse expiry strings with the multi-format parser (`%d-%b-%y`, `%d%b%y`, etc.), sort chronologically, select earliest date on/after the current trading date
    - Raise `ExpiryError` on non-success status, zero expiries, timeout, or all-past
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 5.2 Write property test for weekly-expiry selection
    - **Property 9: Weekly expiry is the earliest date on or after today**
    - **Validates: Requirements 5.2, 5.5**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 9: ...`

  - [x] 5.3 Write edge-case tests for expiry failure modes
    - Unsupported index → error, no `client.expiry()` call, no orders (Req 5.3)
    - Non-success status, zero expiries, and >10s timeout each raise `ExpiryError` and place no entry orders (Req 5.4)
    - _Requirements: 5.3, 5.4_

- [x] 6. Checkpoint - core resolution helpers
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement MTM computation
  - [x] 7.1 Implement per-leg and aggregate MTM
    - `Σ (entry_fill − ltp)·qty` for short legs, `Σ (ltp − entry_fill)·qty` for long legs
    - _Requirements: 13.4_

  - [x] 7.2 Write property test for aggregate MTM sign
    - **Property 20: Aggregate MTM has correct per-leg sign**
    - **Validates: Requirements 13.4**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 20: ...`

- [x] 8. Implement leg stop-loss evaluators (three flavors)
  - [x] 8.1 Implement `Points`, `Percentage`, and `UnderlyingPoints` leg-SL evaluators
    - `Points`: premium ≥ entry_fill + P; `Percentage`: premium ≥ entry_fill × (1 + X/100)
    - `UnderlyingPoints` short CE: spot ≥ entry_spot + U; short PE: spot ≤ entry_spot − U
    - Evaluators are pure predicates returning trigger/no-trigger for use each monitoring cycle
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.7_

  - [x] 8.2 Write property test for leg stop-loss and trailing thresholds
    - **Property 15: Leg stop loss triggers exactly at its threshold**
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4, 11.4**
    - Parameterize across the three static flavors plus the active-trailing case; include premium exactly at threshold
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 15: ...`

- [x] 9. Implement leg trailing stop-loss ratchet
  - [x] 9.1 Implement trailing-SL initialization and ratchet
    - Initialize trail level at the base leg-SL level, or entry fill when no base SL
    - For each complete `I`-point favorable fall, lower trail level by `S` and advance the reference by `I`; trail level is monotone downward only
    - When both base SL and trail apply, enforce the more protective (lower) level each cycle
    - _Requirements: 11.1, 11.2, 11.3, 11.5_

  - [x] 9.2 Write property test for trailing-stop monotonicity and stepping
    - **Property 16: Leg trailing stop is monotone and correctly stepped**
    - **Validates: Requirements 11.2, 11.3, 11.5**
    - Include zero-fall and large-fall paths; assert level non-increasing, advances == complete I-falls, level == initial − advances×S, and min-selection with base SL
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 16: ...`

- [x] 10. Implement overall stop-loss and overall trailing stop-loss
  - [x] 10.1 Implement overall MTM stop and overall trail lock
    - Overall SL: aggregate MTM loss ≥ L triggers all-leg square-off same cycle
    - Overall trail: lock initialized at overall-SL level; raised by `S` per complete `I` improvement above prior peak MTM; breach (MTM ≤ locked level) triggers same-cycle square-off; lock is monotone non-decreasing
    - _Requirements: 13.1, 13.2, 13.3_

  - [x] 10.2 Write property test for overall square-off trigger
    - **Property 21: Overall square-off triggers when MTM breaches the active level**
    - **Validates: Requirements 13.1, 13.3**
    - Include MTM exactly at L; `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 21: ...`

  - [x] 10.3 Write property test for overall trailing lock
    - **Property 22: Overall trailing lock is monotone and correctly stepped**
    - **Validates: Requirements 13.2**
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 22: ...`

- [x] 11. Implement momentum-gated entry evaluator
  - [x] 11.1 Implement `PointsDown` momentum gating
    - Record reference premium at Entry_Time; defer entry until LTP ≤ (reference − N) within the entry–exit window
    - Never place the leg while LTP stays above (reference − N)
    - Missing reference LTP at entry → do not enter, surface error; unmet at exit → do not enter, square off other legs at Exit_Time
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x] 11.2 Write property test for momentum gating
    - **Property 14: Momentum gating enters only after the required fall**
    - **Validates: Requirements 9.2, 9.3**
    - `@settings(max_examples=200)`; tag comment `# Feature: bjp-portfolio-strategies, Property 14: ...`

  - [x] 11.3 Write edge-case tests for momentum boundaries
    - Reference LTP unavailable at entry → no entry, error surfaced (Req 9.4)
    - Momentum unmet at Exit_Time → leg not entered, other legs squared off (Req 9.5)
    - _Requirements: 9.4, 9.5_

- [x] 12. Implement the re-entry manager
  - [x] 12.1 Implement `maybe_reenter()` for Immediate and AtCost
    - `Immediate`: on SL close, if completed count < configured count, re-enter at prevailing market price
    - `AtCost`: after SL close, re-enter at the original entry fill price when premium returns to it
    - Enforce configured count ∈ {1,3,5}; block re-entries at/after the 13:59 IST cutoff (09:15 + 284 min) for NF3/SENSEX3; re-apply leg SL and trail on each re-entry
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [x] 12.2 Write property test for re-entry count bound
    - **Property 17: Re-entry count is bounded**
    - **Validates: Requirements 12.3**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 17: ...`

  - [x] 12.3 Write property test for AtCost re-entry price
    - **Property 18: AtCost re-entry occurs at the original fill price**
    - **Validates: Requirements 12.2**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 18: ...`

  - [x] 12.4 Write property test for re-entry time restriction
    - **Property 19: Re-entry time restriction is enforced**
    - **Validates: Requirements 12.4**
    - Include time exactly at the 13:59 cutoff; `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 19: ...`

  - [x] 12.5 Write unit tests for re-entry re-application
    - Immediate re-entry places at market and re-applies SL/trail to the re-entered position (Req 12.1, 12.5)
    - _Requirements: 12.1, 12.5_

- [x] 13. Implement the risk-evaluation precedence dispatcher
  - [x] 13.1 Implement the ordered per-cycle risk evaluation
    - Evaluate in strict order: momentum entry → leg stop loss → leg trailing stop → overall stop loss → overall trailing stop, acting on the earliest satisfied condition
    - _Requirements: 14.7_

  - [x] 13.2 Write property test for precedence ordering
    - **Property 23: Risk evaluation respects precedence order**
    - **Validates: Requirements 14.7**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 23: ...`

- [~] 14. Checkpoint - pure risk engine complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Implement the bounded retry helper
  - [x] 15.1 Implement a shared bounded-retry utility
    - Cap attempts at 3, stop on first success, preserve already-placed/recorded state; used by entry placement, average-fill retrieval, and square-off
    - _Requirements: 6.5, 7.6, 10.6_

  - [x] 15.2 Write property test for retry bound
    - **Property 11: Retry attempts are bounded by three**
    - **Validates: Requirements 6.5, 7.6, 10.6**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 11: ...`

- [x] 16. Implement the entry executor
  - [x] 16.1 Implement `place_entry()` with multi-leg placement and fill capture
    - Place all non-momentum legs in one `optionsmultiorder` call passing underlying, index exchange, expiry, per-leg offset/option_type/action/quantity/product/pricetype
    - Action SELL for shorts, BUY for hedges; reject invalid actions; product from config else `NRML`
    - Record each leg's traded symbol and order id; missing field → leg failed, retain other leg data
    - Fetch average fill via `client.orderstatus()` up to 3× @2s; failed fill → exclude leg from risk calc, keep symbol/orderid
    - Retry failed placement ≤3×; defer momentum legs to the monitoring loop; enforce `entered_today` (MaxPositionInADay = 1)
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 17.1_

  - [x] 16.2 Write property test for at-most-one-entry-per-day
    - **Property 10: At most one entry per trading day**
    - **Validates: Requirements 6.3**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 10: ...`

  - [x] 16.3 Write property test for default product resolution
    - **Property 12: Default product resolution**
    - **Validates: Requirements 7.3**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 12: ...`

  - [x] 16.4 Write integration test for entry placement (mocked SDK)
    - Assert `optionsmultiorder` payload has correct underlying/exchange/expiry/offset/option_type/action/quantity/product/pricetype (Req 7.1)
    - Assert `orderstatus()` retried for avg fill and entry placed within 5s of trigger (Req 6.2, 7.6)
    - _Requirements: 6.2, 7.1, 7.6_

  - [x] 16.5 Write edge-case tests for entry failures
    - Missing symbol/orderid marks leg failed and retains other leg data (Req 7.5)
    - No avg fill after 3 tries excludes leg from risk calc but keeps symbol/orderid (Req 7.7)
    - Invalid resolved action (not SELL/BUY) rejects entry with logged error (Req 7.2)
    - _Requirements: 7.2, 7.5, 7.7_

- [x] 17. Implement the execution-mode router
  - [x] 17.1 Implement sandbox/live routing
    - Sandbox routes all orders through the Analyzer and never to the broker; live routes to the broker only when explicitly enabled; log the active mode; on live-vs-platform mismatch log and follow the platform's effective mode
    - _Requirements: 16.2, 16.3, 16.4, 16.6_

  - [x] 17.2 Write integration test for effective-mode routing (mocked SDK)
    - Live configured but platform analyzer state prevents live routing → logs mismatch and routes per platform effective mode (Req 16.6)
    - _Requirements: 16.6_

- [x] 18. Implement the monitoring loop
  - [x] 18.1 Implement `monitor()` fixed-cadence loop
    - While any leg is open, fetch LTP per instrument via `client.quotes()` at the configured interval (default 1.0s)
    - On fetch failure: log, retain last-known LTP and leg state, continue next cycle
    - If any open leg has no known LTP, skip aggregate MTM that cycle and surface a stale-data error
    - Invoke the precedence dispatcher each cycle; optional WebSocket mode subscribes over `ws_url`, evaluates per tick, and falls back to polling on connect failure/drop without exiting
    - _Requirements: 14.1, 14.2, 14.4, 14.5, 14.6, 13.6_

  - [x] 18.2 Write integration test for polling loop resilience (mocked SDK)
    - LTP fetch failure logs and continues with last-known state (Req 14.6); missing LTP for an open leg skips MTM with stale-data error (Req 13.6)
    - _Requirements: 13.6, 14.6_

  - [x] 18.3 Write integration test for WebSocket mode and fallback (mocked SDK)
    - WS ticks drive risk evaluation (Req 14.4); connection drop falls back to polling without exiting (Req 14.5)
    - _Requirements: 14.4, 14.5_

- [ ] 19. Implement the exit manager
  - [x] 19.1 Implement square-off-all, scheduled exit, and final summary
    - On overall SL/trail trigger with `square_off_all_legs` true, square off every open leg same cycle; when false, close only designated legs
    - At system time ≥ Exit_Time, square off every open leg regardless of other states
    - When all legs confirmed closed, stop the monitoring loop and log the final realized MTM summary
    - On square-off failure, log the affected leg and re-submit each subsequent cycle until closed; retry remaining legs on partial failure and report which stay open
    - _Requirements: 13.5, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_

  - [-] 19.2 Write property test for square-off scope
    - **Property 24: Square-off scope matches configuration**
    - **Validates: Requirements 15.1, 15.2**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 24: ...`

  - [-] 19.3 Write property test for scheduled exit
    - **Property 25: Scheduled exit closes everything**
    - **Validates: Requirements 15.3**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 25: ...`

  - [-] 19.4 Write property test for hedge no-early-exit behavior
    - **Property 13: Hedge strategies never exit early**
    - **Validates: Requirements 8.3, 8.4**
    - `@settings(max_examples=100)`; tag comment `# Feature: bjp-portfolio-strategies, Property 13: ...`

  - [ ] 19.5 Write edge-case tests for square-off failures
    - Partial overall square-off failure retries remaining legs and reports which stay open (Req 13.5)
    - Scheduled-exit square-off failure re-submits each subsequent cycle until closed (Req 15.6)
    - _Requirements: 13.5, 15.6_

- [ ] 20. Implement the logger and engine orchestration
  - [-] 20.1 Implement the IST logger
    - `log_event(kind, **fields)` writes single-line IST-timestamped records to stdout; place/re-enter/square-off logs symbol/action/quantity/price context; SL/trail/overall triggers log the condition and causing values; SDK errors logged
    - _Requirements: 18.1, 18.2, 18.3, 18.4_

  - [~] 20.2 Implement `StrategyEngine` and `run(config)`
    - Resolve env, validate config, initialize the SDK client with resolved api_key/host/ws_url; on missing API key exit non-zero
    - Log strategy name/index/mode/entry/exit at startup; schedule entry and exit cron jobs on a `BackgroundScheduler` with `Asia/Kolkata`; keep the process alive until exit completes or termination
    - Wire entry executor, execution router, monitoring loop, risk dispatcher, re-entry manager, and exit manager together
    - _Requirements: 1.3, 1.4, 2.6, 6.1, 16.3_

  - [~] 20.3 Write unit tests for startup and log content
    - Startup log contains name/index/mode/entry/exit and active mode (Req 1.4, 16.3); action logs include symbol/action/qty/price and trigger logs include causing values (Req 18.1, 18.2)
    - _Requirements: 1.4, 16.3, 18.1, 18.2_

  - [~] 20.4 Write integration test for scheduler and client init (mocked SDK)
    - Scheduler timezone is `Asia/Kolkata` (Req 6.1); client initialized with resolved api_key/host/ws_url (Req 2.6)
    - _Requirements: 2.6, 6.1_

- [~] 21. Checkpoint - engine wired end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 22. Author the 10 thin per-strategy config scripts
  - [~] 22.1 Create `nf_hedge.py` and `sensex_hedge.py`
    - BUY CE+PE at `OTM20`, 5 lots, entry 09:24, exit 15:26, no SL/target/re-entry; each calls `core.run(CONFIG)` under `__main__`
    - _Requirements: 8.1, 8.2, 17.1, 19.9, 19.10_

  - [~] 22.2 Create `nf1.py` and `sensex1.py`
    - SELL CE+PE at `ITM2`, 2 lots, entry 09:25, exit 15:29; NF1 Points SL 15, trail 70/40, AtCost×1, overall SL 5200, trail 6000/9000; SENSEX1 Points SL 50, trail 230/130, AtCost×1, overall SL 4600, trail 20000/30000
    - Document the hedge dependency (NF/SENSEX HEDGE at 09:24) in each module docstring
    - _Requirements: 17.2, 19.3, 19.6_

  - [~] 22.3 Create `nf2_mean_reversion.py` and `sensex2_mean_reversion.py`
    - SELL CE+PE at `ITM1`, 2 lots, entry 09:16, exit 15:29; NF2 PointsDown 10, Percentage SL 20, trail 20/2, AtCost×1, overall SL 5600, trail 6000/6000; SENSEX2 PointsDown 35, Percentage SL 20, trail 65/5, AtCost×1, overall SL 4900, trail 20000/20000
    - _Requirements: 19.2, 19.5_

  - [~] 22.4 Create `nf3_adjustable_strangle.py` and `sensex3_adjustable_strangle.py`
    - SELL CE+PE at `OTM6`, 1 lot, entry 09:16, exit 15:22; UnderlyingPoints SL (NF3 100, SENSEX3 350), Immediate×3, square-off-all true, reentry cutoff 284; NF3 overall SL 1750, trail 2000/2000; SENSEX3 overall SL 1500, trail 6600/6600
    - _Requirements: 19.1, 19.4_

  - [~] 22.5 Create `nifty_1dte.py` and `sensex_1dte.py`
    - SELL CE+PE, 5 lots, entry 09:18, exit 15:23, UnderlyingPoints SL, Immediate×5, overall SL 5000, square-off-all true, no overall trail; NIFTY `OTM10` SL 100, SENSEX `OTM18` SL 350
    - _Requirements: 19.7, 19.8_

- [ ] 23. Verify deliverable structure and parameter fidelity
  - [~] 23.1 Write table-driven parameter-fidelity smoke test
    - Assert each script's `CONFIG` matches the Per-Strategy Parameter Table exactly: offset, lots, quantity, entry/exit, momentum, leg SL flavor/value, trail I/S, re-entry kind/count, overall SL, overall trail I/S, square-off-all, and re-entry cutoff
    - _Requirements: 8.1, 8.2, 17.1, 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.9, 19.10_

  - [~] 23.2 Write structure/import smoke test
    - Assert exactly 10 strategy files exist with the required names, each imports cleanly, contains no import of any other portfolio strategy script, and each short script's docstring documents its hedge dependency
    - _Requirements: 1.1, 1.2, 17.2_

- [~] 24. Final checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation and the two deliverable-verification smoke tasks (23.1, 23.2) are not optional because they are the acceptance gate for Requirements 1 and 19.
- Each of the 26 correctness properties is implemented by exactly one Hypothesis property test with `@settings(max_examples>=100)`, a mocked/in-memory SDK, and a `# Feature: bjp-portfolio-strategies, Property N: ...` tag comment.
- Property tests are placed immediately after the implementation they validate to catch errors early.
- Production code lives in `strategies/bjp_portfolio/` (Ruff-excluded); tests live under `test/bjp_portfolio/` and must be lint-clean. Run with `uv run pytest`.
- Each task references the specific requirement numbers and design components it implements; checkpoints ensure incremental validation.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "1.2"] },
    { "id": 2, "tasks": ["3.1", "2.2", "2.3"] },
    { "id": 3, "tasks": ["4.1", "3.2", "3.3", "3.4", "3.5"] },
    { "id": 4, "tasks": ["5.1", "4.2", "4.3", "4.4"] },
    { "id": 5, "tasks": ["7.1", "5.2", "5.3"] },
    { "id": 6, "tasks": ["8.1", "7.2"] },
    { "id": 7, "tasks": ["9.1", "8.2"] },
    { "id": 8, "tasks": ["10.1", "9.2"] },
    { "id": 9, "tasks": ["11.1", "10.2", "10.3"] },
    { "id": 10, "tasks": ["12.1", "11.2", "11.3"] },
    { "id": 11, "tasks": ["13.1", "12.2", "12.3", "12.4", "12.5"] },
    { "id": 12, "tasks": ["15.1", "13.2"] },
    { "id": 13, "tasks": ["16.1", "15.2"] },
    { "id": 14, "tasks": ["17.1", "16.2", "16.3", "16.4", "16.5"] },
    { "id": 15, "tasks": ["18.1", "17.2"] },
    { "id": 16, "tasks": ["19.1", "18.2", "18.3"] },
    { "id": 17, "tasks": ["20.1", "19.2", "19.3", "19.4", "19.5"] },
    { "id": 18, "tasks": ["20.2"] },
    { "id": 19, "tasks": ["22.1", "22.2", "22.3", "22.4", "22.5", "20.3", "20.4"] },
    { "id": 20, "tasks": ["23.1", "23.2"] }
  ]
}
```
