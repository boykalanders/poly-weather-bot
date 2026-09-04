"""Common strategy types."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    """A proposal to buy one outcome token. Sizing happens later, in RiskManager."""
    strategy: str
    event_slug: str
    condition_id: str
    token_id: str
    market: str            # human-readable label
    side: str              # always BUY -- we buy the cheap side rather than shorting
    price: float           # limit price we are willing to pay
    model_prob: float      # our estimate of the true probability
    edge: float            # model_prob - price
    available_size: float = 1e9
    note: str = ""
    meta: dict = field(default_factory=dict)


class Strategy:
    name = "base"

    def generate(self) -> list[Signal]:
        raise NotImplementedError
