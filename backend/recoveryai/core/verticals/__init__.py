"""Vertical registry — the agent's only lookup point for specialist config."""

from __future__ import annotations

from recoveryai.core.verticals.autopay import CONFIG as AUTOPAY_CONFIG
from recoveryai.core.verticals.b2b import CONFIG as B2B_CONFIG
from recoveryai.core.verticals.base import VerticalConfig
from recoveryai.core.verticals.cart import CONFIG as CART_CONFIG

VERTICALS: dict[str, VerticalConfig] = {
    CART_CONFIG.name: CART_CONFIG,
    B2B_CONFIG.name: B2B_CONFIG,
    AUTOPAY_CONFIG.name: AUTOPAY_CONFIG,
}


def get_vertical(name: str) -> VerticalConfig:
    try:
        return VERTICALS[name]
    except KeyError as exc:
        raise ValueError(f"unknown vertical {name!r}; known: {sorted(VERTICALS)}") from exc


__all__ = ["VERTICALS", "VerticalConfig", "get_vertical"]
