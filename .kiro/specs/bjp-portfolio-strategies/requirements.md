# Requirements Document

## Introduction

This feature translates the backtested AlgoTest portfolio "BJP COMPLETE TC" (defined in `bjpcompletetc.algtst`) into live-tradable Python strategy scripts that run inside the existing OpenAlgo Python Strategy Host (`/python`). The portfolio contains 10 intraday, same-day options strategies on NIFTY and SENSEX weekly expiries. Each of the 10 strategies is delivered as a separate, independently runnable Python script that uses the OpenAlgo Python SDK for order placement, expiry resolution, and live price monitoring, and APScheduler for IST-based entry and exit scheduling.

The scripts must faithfully reproduce the backtest's per-leg and per-strategy risk controls: strike selection by offset, momentum-gated entry, three flavors of leg stop loss (premium points, premium percentage, underlying points), leg trailing stop loss, leg re-entry (Immediate and AtCost) with counts and time cutoffs, overall MTM stop loss, overall trailing stop loss, and square-off-all-legs behavior. Each script exposes editable configuration (lots, index, entry/exit times, sandbox vs live) and is safe to validate in the OpenAlgo Analyzer/sandbox before live trading.

### Confirmed Decisions

The following decisions were confirmed during review and are reflected in the requirements below:

- **A1 — Lot sizes:** NIFTY Lot_Size is 65 contracts per lot and SENSEX Lot_Size is 20 contracts per lot.
- **A2 — LTP monitoring mechanism:** Monitoring defaults to **polling** via `client.quotes()` on a fixed interval, with WebSocket streaming (`WEBSOCKET_URL`) available as an optional configurable mode.
- **A3 — Execution mode default:** Each script defaults to **sandbox/analyzer mode** (non-live) and requires an explicit configuration flag to trade live.
- **A4 — Polling interval:** The default monitoring interval is 1 second.
- **A5 — ReentryTimeRestriction unit:** The value `284` is minutes from the 09:15 IST market open (09:15 + 284 min = 13:59 IST cutoff for re-entries).

## Glossary

- **OpenAlgo_Platform**: The host application exposing the Python Strategy Host at `/python`, which launches each strategy as an isolated subprocess and injects environment variables.
- **Strategy_Script**: A single standalone Python file implementing exactly one of the 10 portfolio strategies.
- **OpenAlgo_SDK**: The `openalgo` Python package `api` client, initialized with `api_key`, `host`, and optional `ws_url`.
- **Scheduler**: The APScheduler `BackgroundScheduler` configured with the `Asia/Kolkata` (IST) timezone using cron triggers.
- **Leg**: One option position (a CE or PE) within a strategy, with its own strike offset, quantity, action (BUY/SELL), and risk controls.
- **Strike_Offset**: The OpenAlgo option offset string (e.g., `ATM`, `OTM6`, `ITM2`) that selects a strike relative to the at-the-money strike.
- **Strike_Type**: The AlgoTest strike selector from the backtest (e.g., `StrikeType.OTM20`, `StrikeType.ITM1`).
- **Entry_Time**: The IST time at which a strategy places its entry legs.
- **Exit_Time**: The IST time at which a strategy squares off all open legs.
- **Leg_Stop_Loss**: A per-leg stop loss expressed as premium Points, premium Percentage, or UnderlyingPoints (spot move).
- **Leg_Trail_SL**: A per-leg trailing stop loss defined by an InstrumentMove trigger and a StopLossMove step.
- **Leg_Momentum**: A momentum-gated entry condition (`PointsDown N`) requiring the option premium to fall by N points before entering.
- **Leg_Reentry**: Re-entering a leg after its stop loss triggers, either `Immediate` (fresh market entry) or `AtCost` (entry at the original fill price), bounded by a re-entry count.
- **Reentry_Time_Restriction**: A cutoff time after which no further re-entries are permitted.
- **Overall_Stop_Loss**: A strategy-level MTM (mark-to-market) loss threshold that squares off the whole strategy.
- **Overall_Trail_SL**: A strategy-level trailing stop loss defined by an InstrumentMove trigger and a StopLossMove step on aggregate MTM.
- **Square_Off_All_Legs**: Behavior where triggering one exit condition closes all remaining open legs of the strategy.
- **Hedge_Strategy**: A strategy that BUYs OTM CE and PE as tail-risk protection with no stop loss, target, or re-entry.
- **Execution_Mode**: Whether orders are routed live to the broker or to the OpenAlgo Analyzer/sandbox for validation.
- **Lot_Size**: Exchange-defined contracts per lot (NIFTY = 65; SENSEX = 20).
- **MTM**: Mark-to-market profit or loss of open positions, computed from entry fills and current LTP.
- **LTP**: Last traded price of an instrument.

## Requirements

### Requirement 1: Separate Runnable Script Per Strategy

**User Story:** As a trader, I want each of the 10 portfolio strategies delivered as its own standalone Python script, so that I can upload, schedule, start, and stop each strategy independently in the OpenAlgo Python Strategy Host.

#### Acceptance Criteria

1. THE Strategy_Script set SHALL consist of exactly 10 independent Python files, one per portfolio strategy: NF HEDGE, NF3_Adjustable_Strangle, NF2_Mean_reversion, NF1, SENSEX HEDGE, SENSEX3_Adjustable_Strangle, SENSEX2_Mean_reversion, SENSEX1, NIFTY 1DTE, and SENSEX 1DTE.
2. THE Strategy_Script SHALL be executable as a standalone process via `python <script>.py` without importing any other portfolio Strategy_Script.
3. THE Strategy_Script SHALL run continuously until its Exit_Time completes or the process receives a termination signal.
4. WHEN a Strategy_Script starts, THE Strategy_Script SHALL log its strategy name, index, execution mode, entry time, and exit time.

### Requirement 2: Platform Environment Integration

**User Story:** As a platform operator, I want each script to read credentials and endpoints from the environment the way the OpenAlgo Python Strategy Host provides them, so that scripts run unmodified when launched from `/python`.

#### Acceptance Criteria

1. THE Strategy_Script SHALL read the API key from the `OPENALGO_API_KEY` environment variable.
2. WHERE `HOST_SERVER` is set and non-empty, THE Strategy_Script SHALL use its value as the REST host; WHERE `HOST_SERVER` is unset or empty AND `OPENALGO_HOST` is set and non-empty, THE Strategy_Script SHALL use `OPENALGO_HOST`; WHERE both `HOST_SERVER` and `OPENALGO_HOST` are unset or empty, THE Strategy_Script SHALL use `http://127.0.0.1:5000`.
3. WHERE `WEBSOCKET_URL` is set and non-empty, THE Strategy_Script SHALL use its value as the WebSocket endpoint; WHERE `WEBSOCKET_URL` is unset or empty, THE Strategy_Script SHALL construct the WebSocket endpoint from the values of `WEBSOCKET_HOST` and `WEBSOCKET_PORT`.
4. IF WebSocket monitoring is enabled AND `WEBSOCKET_URL` is unset or empty AND either `WEBSOCKET_HOST` or `WEBSOCKET_PORT` is unset or empty, THEN THE Strategy_Script SHALL log an error indicating which WebSocket configuration variable is missing and SHALL fall back to polling per Requirement 14 without exiting.
5. IF the `OPENALGO_API_KEY` environment variable is unset or empty, THEN THE Strategy_Script SHALL log an error message indicating that the API key is missing and SHALL exit with a non-zero exit status without placing orders.
6. WHEN the API key, REST host, and WebSocket endpoint have all been resolved, THE Strategy_Script SHALL initialize the OpenAlgo_SDK client using the resolved API key, host, and WebSocket URL.

### Requirement 3: Per-Strategy Configuration Parameters

**User Story:** As a trader, I want to edit key parameters (lots, index/exchange, entry/exit times, execution mode, monitoring interval) at the top of each script or via environment variables, so that I can tune a strategy without rewriting its logic.

#### Acceptance Criteria

1. THE Strategy_Script SHALL expose the number of lots as an editable configuration value accepting a positive integer from 1 to 100 inclusive.
2. THE Strategy_Script SHALL compute total quantity as the number of lots multiplied by the Lot_Size of the strategy's index, where Lot_Size is 65 for NIFTY and 20 for SENSEX.
3. THE Strategy_Script SHALL expose the index/exchange as an editable configuration value accepting one value from the predefined set of supported index/exchange pairs.
4. THE Strategy_Script SHALL expose Entry_Time and Exit_Time as editable configuration values in IST, each specified in 24-hour HH:MM:SS format ranging from 00:00:00 to 23:59:59.
5. THE Strategy_Script SHALL expose the Execution_Mode as an editable configuration value accepting one value from the predefined set of supported Execution_Mode values.
6. THE Strategy_Script SHALL expose the LTP monitoring interval as an editable configuration value expressed in seconds, accepting an integer from 1 to 300 inclusive.
7. WHERE a configuration value is provided as an environment variable, THE Strategy_Script SHALL use the environment variable value in place of the in-script default value.
8. IF Exit_Time is not later than Entry_Time, THEN THE Strategy_Script SHALL reject the configuration, produce an error indicating the invalid time ordering, and not begin trading.
9. IF any configuration value is outside its permitted range or format, THEN THE Strategy_Script SHALL reject the configuration, produce an error indicating the invalid parameter, and not begin trading.

### Requirement 4: Strike Type to Option Offset Mapping

**User Story:** As a trader, I want the AlgoTest strike selectors reproduced exactly as OpenAlgo option offsets, so that live strike selection matches the backtest.

#### Acceptance Criteria

1. WHEN a strategy selects a strike, THE Strategy_Script SHALL map the backtest Strike_Type to the identically-named OpenAlgo Strike_Offset by preserving the direction token and integer distance (mapping `StrikeType.OTM<n>` to `OTM<n>`, `StrikeType.ITM<n>` to `ITM<n>`, and `StrikeType.ATM` to `ATM`, for all integer distances n from 0 to 50 inclusive), where the integer distance n denotes n strike steps away from the at-the-money reference strike.
2. WHEN a strike offset is applied to a CE leg, THE Strategy_Script SHALL select a strike above the at-the-money reference strike for an OTM offset and below the at-the-money reference strike for an ITM offset, moving one strike step per offset unit.
3. WHEN a strike offset is applied to a PE leg, THE Strategy_Script SHALL select a strike below the at-the-money reference strike for an OTM offset and above the at-the-money reference strike for an ITM offset, moving one strike step per offset unit.
4. WHEN a strategy selects a strike, THE Strategy_Script SHALL derive the at-the-money reference strike from the underlying spot obtained via `client.quotes()` on the index exchange (`NSE_INDEX` for NIFTY, `BSE_INDEX` for SENSEX) by rounding the spot to the nearest multiple of the instrument strike step (50 for NIFTY, 100 for SENSEX), rounding half upward to the higher multiple.
5. IF `client.quotes()` returns an error, an empty response, or no spot price for the index exchange, THEN THE Strategy_Script SHALL abort strike selection without placing any order and SHALL surface an error indicating that the at-the-money reference could not be determined.

### Requirement 5: Weekly Expiry Resolution

**User Story:** As a trader, I want each strategy to resolve the correct weekly expiry at runtime, so that legs are placed on the intended expiry without manual date entry.

#### Acceptance Criteria

1. WHEN a strategy prepares its entry, THE Strategy_Script SHALL invoke `client.expiry()` with the strategy's index, an options instrument type, and the mapped F&O exchange (`NFO` when the index is NIFTY, `BFO` when the index is SENSEX), and SHALL treat a call that does not return within 10 seconds as a resolution failure per criterion 4.
2. WHEN `client.expiry()` returns a success status with one or more expiries, THE Strategy_Script SHALL select as the current-week expiry the expiry whose date is the earliest date on or after the current trading date.
3. IF the mapped index is neither NIFTY nor SENSEX, THEN THE Strategy_Script SHALL log an error indicating the unsupported index and SHALL NOT invoke `client.expiry()` or place entry orders.
4. IF expiry resolution returns a non-success status, returns zero expiries, or does not complete within 10 seconds, THEN THE Strategy_Script SHALL log an error indicating the resolution failure and its cause and SHALL NOT place entry orders.
5. IF every returned expiry date is earlier than the current trading date, THEN THE Strategy_Script SHALL log an error indicating no current or future weekly expiry is available and SHALL NOT place entry orders.

### Requirement 6: Scheduled Entry

**User Story:** As a trader, I want each strategy to place its entry legs automatically at its configured IST entry time, so that live entries match the backtest entry timing.

#### Acceptance Criteria

1. THE Scheduler SHALL be configured with the `Asia/Kolkata` (IST) timezone for all entry trigger evaluations.
2. WHEN the current IST time matches the configured Entry_Time at the hour and minute boundary, THE Strategy_Script SHALL place the strategy's non-momentum entry legs within 5 seconds of the trigger firing.
3. WHILE the strategy already holds one entry position for the current trading day, THE Strategy_Script SHALL reject any additional entry trigger and leave the existing position unchanged, honoring the backtest MaxPositionInADay value of 1.
4. WHERE a leg has a Leg_Momentum condition, THE Strategy_Script SHALL defer that leg's entry until the momentum condition is satisfied per Requirement 9.
5. IF placement of any entry leg fails at the trigger time, THEN THE Strategy_Script SHALL retry the failed leg up to 3 times and, if all retries fail, indicate an entry-placement error identifying the affected leg while preserving any successfully placed legs.

### Requirement 7: Multi-Leg Option Entry

**User Story:** As a trader, I want strategies to enter their CE and PE legs together with the correct action, quantity, product, and offsets, so that the position matches the backtest structure.

#### Acceptance Criteria

1. WHEN entering, THE Strategy_Script SHALL place both the CE and PE legs in a single call to the OpenAlgo_SDK multi-leg option order primitive, passing the resolved expiry, index underlying, index exchange, per-leg offset, option type (CE or PE), action, and a quantity that is a positive integer multiple of the instrument lot size.
2. THE Strategy_Script SHALL set each leg action to SELL for short strategies and to BUY for Hedge_Strategy legs, matching the backtest PositionType, and SHALL reject entry with a logged error indicating an invalid action when the resolved action is neither SELL nor BUY.
3. THE Strategy_Script SHALL set the product type to the configured value when one is provided and to `NRML` when no product type is configured.
4. WHEN entry orders are placed, THE Strategy_Script SHALL read the per-leg results returned by the order response and SHALL record each leg's traded symbol and order id.
5. IF an entry leg's order response omits either the traded symbol or the order id, THEN THE Strategy_Script SHALL treat that leg as failed, SHALL log a failure indicating the missing field, and SHALL retain any successfully recorded leg data.
6. WHEN an entry leg has a recorded order id, THE Strategy_Script SHALL fetch that leg's average fill price using `client.orderstatus()`, retrying up to 3 times with a 2-second interval between attempts until a non-null average fill price is returned, for use in stop-loss and re-entry calculations.
7. IF `client.orderstatus()` fails to return a non-null average fill price for a leg after 3 attempts, THEN THE Strategy_Script SHALL log the failure, SHALL exclude that leg from stop-loss and re-entry calculations, and SHALL preserve the recorded symbol and order id for that leg.
8. IF an entry leg order fails, THEN THE Strategy_Script SHALL log the failure and SHALL evaluate remaining risk controls only against legs that were successfully placed and filled.

### Requirement 8: Hedge Strategy Behavior

**User Story:** As a trader, I want the two hedge strategies to buy protective OTM legs with no stop loss, target, or re-entry, so that tail-risk protection stays in place for the full session.

#### Acceptance Criteria

1. THE NF HEDGE Strategy_Script SHALL BUY a NIFTY CE and a NIFTY PE at offset `OTM20` at entry time 09:24 IST and square off at 15:26 IST.
2. THE SENSEX HEDGE Strategy_Script SHALL BUY a SENSEX CE and a SENSEX PE at offset `OTM20` at entry time 09:24 IST and square off at 15:26 IST.
3. THE Hedge_Strategy Strategy_Script SHALL NOT apply any Leg_Stop_Loss, Leg_Trail_SL, Leg_Reentry, Overall_Stop_Loss, or Overall_Trail_SL.
4. WHILE a Hedge_Strategy holds open legs, THE Strategy_Script SHALL keep those legs open until Exit_Time.

### Requirement 9: Momentum-Gated Entry

**User Story:** As a trader, I want the mean-reversion strategies to enter a leg only after its premium falls by the configured points, so that live entries reproduce the backtest PointsDown momentum behavior.

#### Acceptance Criteria

1. WHERE a leg defines a `PointsDown` Leg_Momentum of N points (N a configured positive value, e.g., 10 for NF2 and 35 for SENSEX2), WHEN Entry_Time is reached, THE Strategy_Script SHALL record the leg's LTP as its reference premium.
2. WHILE the momentum condition (LTP fallen by at least N points below the reference premium) is unmet and the current time is between Entry_Time and Exit_Time, THE Strategy_Script SHALL monitor the candidate leg's LTP on each received price update and SHALL NOT place that leg's entry order.
3. WHEN the candidate leg's LTP is at or below (reference premium minus N points) during the window between Entry_Time and Exit_Time, THE Strategy_Script SHALL place that leg's entry order.
4. IF the reference premium cannot be recorded at Entry_Time because no valid LTP is available for the candidate leg, THEN THE Strategy_Script SHALL NOT place that leg's entry order and SHALL surface an error indication that the leg's reference premium is unavailable.
5. IF the momentum condition remains unmet at Exit_Time, THEN THE Strategy_Script SHALL NOT enter that leg and SHALL square off all other open legs at Exit_Time.

### Requirement 10: Leg Stop Loss (Premium Points, Percentage, Underlying Points)

**User Story:** As a trader, I want each leg's stop loss enforced in the exact flavor from the backtest, so that per-leg risk matches the tested configuration.

#### Acceptance Criteria

1. WHILE a leg is open with a `Points` Leg_Stop_Loss of P points, THE Strategy_Script SHALL square off that leg WHEN the leg premium rises to or above the leg's entry fill price plus P points (adverse direction for a SELL leg being a rise in premium).
2. WHILE a leg is open with a `Percentage` Leg_Stop_Loss of X percent, THE Strategy_Script SHALL square off that leg WHEN the leg premium rises to or above the leg's entry fill price multiplied by (1 + X/100).
3. WHILE a leg is open with an `UnderlyingPoints` Leg_Stop_Loss of U points AND the leg is a short CE, THE Strategy_Script SHALL square off that leg WHEN the underlying spot rises to or above the underlying level recorded at leg entry plus U points.
4. WHILE a leg is open with an `UnderlyingPoints` Leg_Stop_Loss of U points AND the leg is a short PE, THE Strategy_Script SHALL square off that leg WHEN the underlying spot falls to or below the underlying level recorded at leg entry minus U points.
5. WHEN a Leg_Stop_Loss trigger condition is met, THE Strategy_Script SHALL place a squaring-off order for that leg and, upon order confirmation, SHALL record the leg as closed with its closing state retained.
6. IF a squaring-off order placed on a Leg_Stop_Loss trigger fails or is rejected, THEN THE Strategy_Script SHALL retry the squaring-off order up to a maximum of 3 attempts and, if all attempts fail, SHALL keep the leg marked as open and raise an error indication reporting the square-off failure.
7. THE Strategy_Script SHALL evaluate each open leg's stop loss on every monitoring cycle defined in Requirement 14.

### Requirement 11: Leg Trailing Stop Loss

**User Story:** As a trader, I want leg trailing stop losses that lock in gains as the premium falls, so that profitable short legs trail per the backtest InstrumentMove/StopLossMove settings.

#### Acceptance Criteria

1. WHERE a leg defines a `Points` Leg_Trail_SL with InstrumentMove I (I greater than 0) and StopLossMove S (0 less than S and S less than or equal to I), THE Strategy_Script SHALL initialize the leg's trailing stop level to the leg's base Leg_Stop_Loss level, or to the leg's entry fill price when no base Leg_Stop_Loss is defined.
2. WHEN a short leg's premium falls by a complete increment of I points below the reference level at which the trailing stop was last advanced, THE Strategy_Script SHALL lower the leg's trailing stop level by S points and advance the reference level downward by I points, repeating for each further complete I-point fall.
3. THE Strategy_Script SHALL never raise (loosen) a short leg's trailing stop level once set; the stop level SHALL only move in the favorable (downward) direction.
4. WHILE the trailing stop is active, THE Strategy_Script SHALL square off the leg WHEN the leg premium rises to or above the current trailing stop level, recording the exit reason as a trailing stop.
5. WHERE both a base Leg_Stop_Loss and a Leg_Trail_SL apply to a short leg, THE Strategy_Script SHALL enforce whichever stop level is the lower premium (more protective) on each monitoring cycle.

### Requirement 12: Leg Re-entry

**User Story:** As a trader, I want a leg to re-enter after its stop loss triggers, in the Immediate or AtCost style with the configured count and time cutoff, so that re-entries reproduce the backtest.

#### Acceptance Criteria

1. WHEN a leg with an `Immediate` Leg_Reentry closes on stop loss and the leg's completed re-entry count is less than its configured re-entry count, THE Strategy_Script SHALL re-enter the leg at the prevailing market price at the time the stop loss closes the leg.
2. WHILE a leg with an `AtCost` Leg_Reentry has closed on stop loss and its completed re-entry count is less than its configured re-entry count, WHEN the leg's premium returns to its original entry fill price, THE Strategy_Script SHALL re-enter the leg at that original entry fill price.
3. THE Strategy_Script SHALL limit re-entries per leg to the configured re-entry count, where the configured count is one of 1, 3, or 5.
4. IF the current time is at or after the Reentry_Time_Restriction cutoff of 284 minutes after the 09:15 IST market open (13:59 IST) for NF3 and SENSEX3 instruments, THEN THE Strategy_Script SHALL NOT place any further re-entries for the affected legs.
5. WHEN a leg re-enters, THE Strategy_Script SHALL re-apply that leg's Leg_Stop_Loss and Leg_Trail_SL to the re-entered position.

### Requirement 13: Overall Stop Loss and Overall Trailing Stop Loss

**User Story:** As a trader, I want strategy-level MTM stop loss and trailing stop loss that close the entire strategy, so that aggregate risk matches the backtest.

#### Acceptance Criteria

1. WHILE a strategy has open legs and defines an `MTM` Overall_Stop_Loss of L rupees, WHEN the aggregate MTM loss is greater than or equal to L, THE Strategy_Script SHALL square off all open legs within the same monitoring cycle in which the threshold is detected.
2. WHERE a strategy defines a `Points` Overall_Trail_SL with InstrumentMove I and StopLossMove S, THE Strategy_Script SHALL initialize the locked MTM stop at the configured Overall_Stop_Loss level and raise the locked MTM stop by S for each complete increment of I by which the aggregate MTM improves above its previously recorded peak.
3. WHEN the aggregate MTM falls to or below the locked Overall_Trail_SL level, THE Strategy_Script SHALL square off all open legs within the same monitoring cycle in which the breach is detected.
4. THE Strategy_Script SHALL compute aggregate MTM on every monitoring cycle as the sum across all open legs of (entry fill price minus current LTP) multiplied by quantity for short legs, and (current LTP minus entry fill price) multiplied by quantity for long legs.
5. IF a square-off order for any leg fails while closing the strategy, THEN THE Strategy_Script SHALL retry the square-off for each remaining open leg and surface an error indicating which legs remain open.
6. IF the current LTP is unavailable for any open leg during a monitoring cycle, THEN THE Strategy_Script SHALL skip the aggregate MTM evaluation for that cycle without squaring off and surface an error indicating stale or missing market data.

### Requirement 14: Live Price Monitoring Loop

**User Story:** As a trader, I want a monitoring loop that evaluates all live risk conditions on a fixed cadence, so that stop losses, trails, momentum, and overall limits are checked continuously while legs are open.

#### Acceptance Criteria

1. WHILE any leg is open, THE Strategy_Script SHALL fetch the current LTP for each monitored instrument on each monitoring cycle.
2. THE Strategy_Script SHALL default to polling LTP via `client.quotes()` at the configured monitoring interval, defaulting to 1 second when unconfigured and accepting configured values in the range 0.1 to 60 seconds inclusive.
3. IF the configured monitoring interval is outside the range 0.1 to 60 seconds or is non-numeric, THEN THE Strategy_Script SHALL log an error indicating an invalid interval and SHALL apply the default interval of 1 second.
4. WHERE WebSocket monitoring is enabled in configuration, THE Strategy_Script SHALL subscribe to the required instruments over `WEBSOCKET_URL` and SHALL evaluate risk conditions on each received tick.
5. WHERE WebSocket monitoring is enabled, IF the WebSocket connection fails to establish or drops while any leg is open, THEN THE Strategy_Script SHALL log the error and SHALL fall back to polling LTP via `client.quotes()` at the configured monitoring interval without exiting.
6. IF an LTP fetch fails on a monitoring cycle, THEN THE Strategy_Script SHALL log the error, SHALL retain the last known LTP and open-leg state, and SHALL continue monitoring on the next cycle without exiting.
7. WHEN a monitoring cycle begins, THE Strategy_Script SHALL evaluate momentum entry, leg stop loss, leg trailing stop, overall stop loss, and overall trailing stop in that precedence order.

### Requirement 15: Square-Off-All-Legs and Scheduled Exit

**User Story:** As a trader, I want the strategy to close remaining legs when square-off-all is configured and to force-close everything at the exit time, so that no position is left open past the intended session window.

#### Acceptance Criteria

1. WHERE a strategy defines Square_Off_All_Legs as true, WHEN an Overall_Stop_Loss or Overall_Trail_SL trigger condition is met, THE Strategy_Script SHALL submit square-off orders for every open leg of the strategy within the same monitoring cycle in which the trigger is detected.
2. WHERE a strategy defines Square_Off_All_Legs as false, WHEN an Overall_Stop_Loss or Overall_Trail_SL trigger condition is met, THE Strategy_Script SHALL square off only the legs designated for that trigger and SHALL leave all other open legs unchanged.
3. WHEN the current system time is greater than or equal to the configured Exit_Time, THE Strategy_Script SHALL submit square-off orders for every open leg of the strategy regardless of Square_Off_All_Legs, Overall_Stop_Loss, or Overall_Trail_SL states.
4. WHEN every leg of the strategy is confirmed closed, THE Strategy_Script SHALL stop the monitoring loop.
5. WHEN the monitoring loop stops, THE Strategy_Script SHALL log a final MTM summary containing the realized MTM value for the strategy.
6. IF a square-off order fails, THEN THE Strategy_Script SHALL log a failure entry indicating the affected leg and SHALL re-submit the square-off for that leg on each subsequent monitoring cycle until the leg is confirmed closed or the process is terminated, while leaving positions of successfully closed legs unchanged.

### Requirement 16: Execution Mode Safety

**User Story:** As a trader, I want to validate each strategy in the OpenAlgo Analyzer/sandbox before trading live, so that I can confirm behavior without risking capital.

#### Acceptance Criteria

1. THE Strategy_Script SHALL default to sandbox/analyzer Execution_Mode when the Execution_Mode is unconfigured.
2. WHILE in sandbox/analyzer Execution_Mode, THE Strategy_Script SHALL route all orders through the OpenAlgo Analyzer/sandbox and SHALL NOT route orders to the live broker.
3. WHEN a Strategy_Script starts, THE Strategy_Script SHALL log the active Execution_Mode as either sandbox or live.
4. WHERE live Execution_Mode is explicitly enabled in configuration, THE Strategy_Script SHALL route orders to the live broker.
5. IF the configured Execution_Mode value is not one of the supported values (sandbox or live), THEN THE Strategy_Script SHALL reject the configuration, log an error indicating the invalid Execution_Mode, and SHALL NOT place orders.
6. IF live Execution_Mode is configured but the OpenAlgo platform analyzer state prevents live routing, THEN THE Strategy_Script SHALL log the mismatch and route orders according to the platform's effective mode.

### Requirement 17: Hedge-First Entry Ordering for Short Strategies

**User Story:** As a risk-conscious trader, I want protective hedge legs to be placeable before or independently of short-selling strategies, so that naked short exposure is minimized during entry.

#### Acceptance Criteria

1. THE NF HEDGE and SENSEX HEDGE Strategy_Scripts SHALL use Entry_Time 09:24 IST so that hedge protection is established before the later-entering short strategies (09:25 IST for NF1 and SENSEX1).
2. WHERE a short strategy shares an index with a Hedge_Strategy, THE short Strategy_Script SHALL document its dependency on the corresponding Hedge_Strategy being active for tail-risk protection.

### Requirement 18: Observability and Logging

**User Story:** As a trader, I want each script to log entries, exits, stop-loss triggers, re-entries, and errors with timestamps, so that I can audit behavior against the backtest using the platform log viewer.

#### Acceptance Criteria

1. WHEN the Strategy_Script places, re-enters, or squares off any leg, THE Strategy_Script SHALL log the action with an IST timestamp, symbol, action, quantity, and price context.
2. WHEN any Leg_Stop_Loss, Leg_Trail_SL, Overall_Stop_Loss, or Overall_Trail_SL triggers, THE Strategy_Script SHALL log which condition triggered and the values that caused it.
3. IF any SDK call raises an error, THEN THE Strategy_Script SHALL log the error message and continue per the relevant requirement's error handling.
4. THE Strategy_Script SHALL write logs to standard output so the OpenAlgo Python Strategy Host captures them in `logs/strategies/`.

### Requirement 19: Per-Strategy Parameter Fidelity

**User Story:** As a trader, I want each of the 10 scripts to encode the exact backtest parameters for its strategy, so that live behavior matches "BJP COMPLETE TC".

#### Acceptance Criteria

1. THE NF3_Adjustable_Strangle Strategy_Script SHALL SELL NIFTY CE and PE at `OTM6`, qty 1 lot, entry 09:16, exit 15:22, with UnderlyingPoints Leg_Stop_Loss 100, Immediate Leg_Reentry count 3, Overall_Stop_Loss MTM 1750, Overall_Trail_SL Points 2000/2000, Square_Off_All_Legs true, and Reentry_Time_Restriction 284.
2. THE NF2_Mean_reversion Strategy_Script SHALL SELL NIFTY CE and PE at `ITM1`, qty 2 lots, entry 09:16, exit 15:29, with PointsDown Leg_Momentum 10, AtCost Leg_Reentry count 1, Percentage Leg_Stop_Loss 20, Points Leg_Trail_SL 20/2, Overall_Stop_Loss MTM 5600, and Overall_Trail_SL Points 6000/6000.
3. THE NF1 Strategy_Script SHALL SELL NIFTY CE and PE at `ITM2`, qty 2 lots, entry 09:25, exit 15:29, with Points Leg_Stop_Loss 15, AtCost Leg_Reentry count 1, Points Leg_Trail_SL 70/40, Overall_Stop_Loss MTM 5200, and Overall_Trail_SL Points 6000/9000.
4. THE SENSEX3_Adjustable_Strangle Strategy_Script SHALL SELL SENSEX CE and PE at `OTM6`, qty 1 lot, entry 09:16, exit 15:22, with UnderlyingPoints Leg_Stop_Loss 350, Immediate Leg_Reentry count 3, Overall_Stop_Loss MTM 1500, Overall_Trail_SL Points 6600/6600, Square_Off_All_Legs true, and Reentry_Time_Restriction 284.
5. THE SENSEX2_Mean_reversion Strategy_Script SHALL SELL SENSEX CE and PE at `ITM1`, qty 2 lots, entry 09:16, exit 15:29, with PointsDown Leg_Momentum 35, AtCost Leg_Reentry count 1, Percentage Leg_Stop_Loss 20, Points Leg_Trail_SL 65/5, Overall_Stop_Loss MTM 4900, and Overall_Trail_SL Points 20000/20000.
6. THE SENSEX1 Strategy_Script SHALL SELL SENSEX CE and PE at `ITM2`, qty 2 lots, entry 09:25, exit 15:29, with Points Leg_Stop_Loss 50, AtCost Leg_Reentry count 1, Points Leg_Trail_SL 230/130, Overall_Stop_Loss MTM 4600, and Overall_Trail_SL Points 20000/30000.
7. THE NIFTY 1DTE Strategy_Script SHALL SELL NIFTY CE and PE at `OTM10`, qty 5 lots, entry 09:18, exit 15:23, with UnderlyingPoints Leg_Stop_Loss 100, Immediate Leg_Reentry count 5, Overall_Stop_Loss MTM 5000, and Square_Off_All_Legs true.
8. THE SENSEX 1DTE Strategy_Script SHALL SELL SENSEX CE and PE at `OTM18`, qty 5 lots, entry 09:18, exit 15:23, with UnderlyingPoints Leg_Stop_Loss 350, Immediate Leg_Reentry count 5, Overall_Stop_Loss MTM 5000, and Square_Off_All_Legs true.
9. THE NF HEDGE Strategy_Script SHALL BUY NIFTY CE and PE at `OTM20`, qty 5 lots, entry 09:24, exit 15:26, with no stop loss, target, or re-entry.
10. THE SENSEX HEDGE Strategy_Script SHALL BUY SENSEX CE and PE at `OTM20`, qty 5 lots, entry 09:24, exit 15:26, with no stop loss, target, or re-entry.
