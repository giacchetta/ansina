"""The autonomic tick loop: the Heart's one day-one duty. See issue #11.

`TickLoop` is the scheduler; `heart.tick.snapshot` builds the bounded state snapshot
every tick sends to the Heart, and `heart.tick.decision` turns its raw reply into a
`TickDecision`. `api/app.py`'s lifespan owns construction, `start()`, and `stop()`, the
same way it owns `HeartRuntime` and `Database`.
"""

from ansina.heart.tick.brain_escalation_handler import BrainEscalationHandler
from ansina.heart.tick.decision import TickDecision, parse_decision
from ansina.heart.tick.journal_handler import JournalDecisionHandler
from ansina.heart.tick.loop import (
    CompositeDecisionHandler,
    DecisionHandler,
    LoggingDecisionHandler,
    TickController,
    TickLifecycle,
    TickLoop,
    TickLoopFactory,
    TickOutcome,
    build_tick_loop,
)
from ansina.heart.tick.snapshot import (
    SnapshotItem,
    StateSnapshotSource,
    TickPrompt,
    build_prompt,
    collect_items,
)
from ansina.heart.tick.sources import DaemonStateSource, RecentJournalSource

__all__ = [
    "BrainEscalationHandler",
    "CompositeDecisionHandler",
    "DaemonStateSource",
    "DecisionHandler",
    "JournalDecisionHandler",
    "LoggingDecisionHandler",
    "RecentJournalSource",
    "SnapshotItem",
    "StateSnapshotSource",
    "TickController",
    "TickDecision",
    "TickLifecycle",
    "TickLoop",
    "TickLoopFactory",
    "TickOutcome",
    "TickPrompt",
    "build_prompt",
    "build_tick_loop",
    "collect_items",
    "parse_decision",
]
