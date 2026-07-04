#!/usr/bin/env python
"""SENSEX3_Adjustable_Strangle: short SENSEX OTM6 CE+PE adjustable strangle.

Sells CE+PE at OTM6 with UnderlyingPoints leg SL 350, no leg trail,
Immediate re-entry ×3, overall SL 1500, overall trail 6600/6600.
Square-off-all is true. Re-entry cutoff at 284 minutes (13:59 IST)
(Req 19.4).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="SENSEX3_Adjustable_Strangle",
    index=core.SENSEX,
    lots=1,
    entry_time="09:16:00",
    exit_time="15:22:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "OTM6",
            stop_loss=core.LegStopLoss(core.SLKind.UNDERLYING_POINTS, 350),
            reentry=core.LegReentry(core.ReentryKind.IMMEDIATE, 3),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "OTM6",
            stop_loss=core.LegStopLoss(core.SLKind.UNDERLYING_POINTS, 350),
            reentry=core.LegReentry(core.ReentryKind.IMMEDIATE, 3),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(1500),
    overall_trail_sl=core.OverallTrailSL(6600, 6600),
    square_off_all_legs=True,
    reentry_time_restriction_min=284,
)

if __name__ == "__main__":
    core.run(CONFIG)
