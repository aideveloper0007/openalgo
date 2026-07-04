#!/usr/bin/env python
"""SENSEX HEDGE: long SENSEX OTM20 CE+PE protective hedge.

This hedge strategy buys deep out-of-the-money SENSEX CE and PE options to provide
tail-risk protection for the short strategies in the BJP portfolio. It applies no
leg stop-loss, trailing stop, re-entry, overall stop-loss, or overall trailing
stop-loss, and holds both legs until Exit_Time (Req 8.1, 8.3, 8.4).

No hedge dependency — this strategy is itself the hedge. SENSEX1 (09:25 IST)
relies on this hedge being active (entered at 09:24 IST) for tail-risk
protection (Req 17.2).
"""
import bjp_core as core

CONFIG = core.StrategyConfig(
    strategy_name="SENSEX_HEDGE",
    index=core.SENSEX,
    lots=5,
    entry_time="09:24:00",
    exit_time="15:26:00",
    legs=[
        core.LegConfig(core.OptionType.CE, core.Action.BUY, "OTM20"),
        core.LegConfig(core.OptionType.PE, core.Action.BUY, "OTM20"),
    ],
)

if __name__ == "__main__":
    core.run(CONFIG)
