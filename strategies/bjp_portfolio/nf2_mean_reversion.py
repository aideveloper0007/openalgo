#!/usr/bin/env python
"""NF2_Mean_reversion: short NIFTY ITM1 CE+PE with PointsDown momentum gating.

Sells CE+PE at ITM1 with PointsDown 10 momentum gating, Percentage leg SL 20,
leg trail 20/2, AtCost re-entry ×1, overall SL 5600, overall trail 6000/6000.
Square-off-all is false (Req 19.5).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="NF2_Mean_reversion",
    index=core.NIFTY,
    lots=2,
    entry_time="09:16:00",
    exit_time="15:29:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "ITM1",
            stop_loss=core.LegStopLoss(core.SLKind.PERCENTAGE, 20),
            trail_sl=core.LegTrailSL(20, 2),
            momentum=core.LegMomentum(10),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "ITM1",
            stop_loss=core.LegStopLoss(core.SLKind.PERCENTAGE, 20),
            trail_sl=core.LegTrailSL(20, 2),
            momentum=core.LegMomentum(10),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(5600),
    overall_trail_sl=core.OverallTrailSL(6000, 6000),
    square_off_all_legs=False,
)

if __name__ == "__main__":
    core.run(CONFIG)
