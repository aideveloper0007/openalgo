#!/usr/bin/env python
"""NF1: short NIFTY ITM2 CE+PE strangle.

Depends on NF HEDGE (09:24 IST) being active for tail-risk protection (Req 17.2).

Sells CE+PE at ITM2 with Points leg SL 15, leg trail 70/40, AtCost re-entry ×1,
overall SL 5200, overall trail 6000/9000. Square-off-all is false (Req 19.3).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="NF1",
    index=core.NIFTY,
    lots=2,
    entry_time="09:25:00",
    exit_time="15:29:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "ITM2",
            stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15),
            trail_sl=core.LegTrailSL(70, 40),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "ITM2",
            stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15),
            trail_sl=core.LegTrailSL(70, 40),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(5200),
    overall_trail_sl=core.OverallTrailSL(6000, 9000),
    square_off_all_legs=False,
)

if __name__ == "__main__":
    core.run(CONFIG)
