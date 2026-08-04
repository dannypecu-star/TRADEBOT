"""Common types for platform adapters.

A :class:`Signal` is the neutral instruction the strategy layer produces. Each adapter
knows how to turn it into a venue-specific action. Adapters share one rule: **dry-run by
default**. Nothing sends a real order unless the caller explicitly opts in, so wiring up a
new venue can be tested end-to-end without touching a live account.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Signal:
    """A platform-neutral trade instruction derived from a strategy decision."""

    symbol: str
    action: str                 # "enter_long" | "exit" | "hold"
    strength: float = 1.0       # target position in [0, 1]
    price: float | None = None  # reference price at decision time (for logging)
    strategy: str = "unknown"
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def signal_from_position(
    symbol: str,
    prev_position: float,
    target_position: float,
    price: float | None,
    strategy: str,
) -> Signal:
    """Diff the previous and target positions into a discrete action.

    This is the bridge between the continuous [0,1] position series the backtester/paper
    trader use and the discrete open/close events a hosted bot platform consumes.
    """
    was_long = prev_position > 0
    want_long = target_position > 0
    if want_long and not was_long:
        action = "enter_long"
    elif was_long and not want_long:
        action = "exit"
    else:
        action = "hold"
    return Signal(
        symbol=symbol, action=action, strength=target_position,
        price=price, strategy=strategy,
    )


class PlatformAdapter(ABC):
    """Base class: adapters send a :class:`Signal` to a specific venue."""

    name: str = "base"

    def __init__(self, dry_run: bool = True):
        # dry_run True => build and return the payload but never transmit it.
        self.dry_run = dry_run

    @abstractmethod
    def send(self, signal: Signal) -> dict:
        """Send (or, in dry-run, just build) the venue-specific request.

        Returns a dict describing what was (or would be) sent, so callers can log and
        assert on it in tests without a live account.
        """
        raise NotImplementedError
