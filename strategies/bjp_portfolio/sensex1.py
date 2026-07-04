#!/usr/bin/env python
"""SENSEX1: short SENSEX ITM2 CE+PE strangle.

Depends on SENSEX HEDGE (09:24 IST) being active for tail-risk protection
(Req 17.2).

Sells CE+PE at ITM2 with Points leg SL 50, leg trail 230/130, AtCost re-entry
×1, overall SL 4600, overall trail 20000/30000. Square-off-all is false
(Req 19.6).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="SENSEX1",
    index=core.SENSEX,
    lots=2,
    entry_time="09:25:00",
    exit_time="15:29:00",
    legs=[
        core.LegConfig(
            core.OptionType.CE, core.Action.SELL, "ITM2",
            stop_loss=core.LegStopLoss(core.SLKind.POINTS, 50),
            trail_sl=core.LegTrailSL(230, 130),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
        core.LegConfig(
            core.OptionType.PE, core.Action.SELL, "ITM2",
            stop_loss=core.LegStopLoss(core.SLKind.POINTS, 50),
            trail_sl=core.LegTrailSL(230, 130),
            reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
        ),
    ],
    overall_stop_loss=core.OverallStopLoss(4600),
    overall_trail_sl=core.OverallTrailSL(20000, 30000),
    square_off_all_legs=False,
)

if __name__ == "__main__":
    core.run(CONFIG)
