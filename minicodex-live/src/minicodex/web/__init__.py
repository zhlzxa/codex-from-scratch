"""The browser front end for this agent: `minicodex serve`.

The outermost layer of the package, beside `__main__` rather than under it.
Nothing in `src/minicodex/*.py` imports anything from here -- that direction is
what `scripts/check_layers.py` checks, and it is why the console could be
written at all without touching the agent: every seam it needs was already
there for another module's reason.

    approval.Approver                        ->  a browser instead of `input()`
    rollout.RolloutWriter                    ->  live events, fsynced first
    rollout.resolve("last")                  ->  one conversation per thread
    composition.top_level_tools              ->  one place that assembles a run
"""

from __future__ import annotations

from .app import Console, create_app, serve
from .store import DEFAULT_DATA_DIR, Store

__all__ = ["DEFAULT_DATA_DIR", "Console", "Store", "create_app", "serve"]
