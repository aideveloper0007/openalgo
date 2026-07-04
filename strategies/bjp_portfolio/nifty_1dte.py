#!/usr/bin/env python
"""NIFTY 1DTE: short NIFTY OTM10 CE+PE one-day-to-expiry strangle.

Sells CE+PE at OTM10 with UnderlyingPoints leg SL 100, no leg trail,
Immediate re-entry ×5, overall SL 5000, no overall trail.
Square-off-all is true (Req 19.7).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="NIFTY_1DTE",
    index=core.NIFTY,
    lots=5,
    entry_time="09:18:00",
    exit_time="15:23:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "OTM10",
            stop_loss=core.LegStopLoss(core.SLKind.UNDERLYING_POINTS, 100),
            reentry=core.LegReentry(core.ReentryKind.IMMEDIATE, 5),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "OTM10",
            stop_loss=core.LegStopLoss(core.SLKind.UNDERLYING_POINTS, 100),
            reentry=core.LegReentry(core.ReentryKind.IMMEDIATE, 5),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(5000),
    square_off_all_legs=True,
)

if __name__ == "__main__":
    core.run(CONFIG)
