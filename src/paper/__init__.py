"""Paper trading: run a strategy against real prices with simulated (fake-money) fills."""
from .broker import PaperBroker, PaperFill
from .paper_trader import PaperTrader, PaperConfig

__all__ = ["PaperBroker", "PaperFill", "PaperTrader", "PaperConfig"]
