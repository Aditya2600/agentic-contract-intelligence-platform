"""A declared, versioned $/million-token price table -- config, not a call-site constant.

Loaded fresh on every call rather than cached at import time: the table is small, reading
it costs nothing next to a model call, and a reviewer editing prices should not need to
restart the process to see the change reflected in the next report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from doctask.config import settings


class PriceTableError(RuntimeError):
    """The declared price table could not be read or does not have the shape it must."""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True, slots=True)
class PriceTable:
    version: str
    prices: dict[str, ModelPrice]

    def cost_usd(self, model: str | None, *, tokens_in: int, tokens_out: int) -> float | None:
        """The spend `model` produced, or `None` when this table names no price for it.

        `None` is the whole point: a model absent from the table is unpriced, not free,
        and a caller that turned this into `0.0` would make the two indistinguishable.
        """
        if model is None:
            return None
        price = self.prices.get(model)
        if price is None:
            return None
        return (
            tokens_in / 1_000_000 * price.input_per_million_usd
            + tokens_out / 1_000_000 * price.output_per_million_usd
        )


def load_price_table(path: str | None = None) -> PriceTable:
    file_path = Path(path or settings.price_table_path)
    try:
        raw = json.loads(file_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PriceTableError(f"could not read price table at {file_path}: {exc}") from exc
    try:
        version = str(raw["version"])
        prices = {
            name: ModelPrice(
                input_per_million_usd=float(entry["input_per_million_usd"]),
                output_per_million_usd=float(entry["output_per_million_usd"]),
            )
            for name, entry in raw["prices"].items()
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PriceTableError(f"price table at {file_path} is malformed: {exc}") from exc
    return PriceTable(version=version, prices=prices)
