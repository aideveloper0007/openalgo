#!/usr/bin/env python
"""SENSEX2_Mean_reversion: short SENSEX ITM1 CE+PE with PointsDown momentum gating.

Sells CE+PE at ITM1 with PointsDown 35 momentum gating, Percentage leg SL 20,
leg trail 65/5, AtCost re-entry ×1, overall SL 4900, overall trail 20000/20000.
Square-off-all is false (Req 19.5).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="SENSEX2_Mean_reversion",
    index=core.SENSEX,
    lots=2,
    entry_time="09:16:00",
    exit_time="15:29:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "ITM1",
            stop_loss=core.LegStopLoss(core.SLKind.PERCENTAGE, 20),
            trail_sl=core.LegTrailSL(65, 5),
            momentum=core.LegMomentum(35),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "ITM1",
            stop_loss=core.LegStopLoss(core.SLKind.PERCENTAGE, 20),
            trail_sl=core.LegTrailSL(65, 5),
            momentum=core.LegMomentum(35),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(4900),
    overall_trail_sl=core.OverallTrailSL(20000, 20000),
    square_off_all_legs=False,
)

if __name__ == "__main__":
    core.run(CONFIG)
