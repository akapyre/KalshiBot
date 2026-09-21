"""Risk gate: fixed stake sizing plus the (currently disabled) daily-loss and
concurrent-position caps. Both caps read as `null` from config/risk.yaml today
per your instructions -- flip them to a number later with no code changes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RiskConfig:
    stake_usd: float
    daily_loss_cap_usd: float | None
    max_concurrent_positions: int | None
    trading_enabled_env_var: str

    @classmethod
    def load(cls, path: str | Path = "config/risk.yaml") -> "RiskConfig":
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
        return cls(
            stake_usd=float(raw["stake_usd"]),
            daily_loss_cap_usd=raw.get("daily_loss_cap_usd"),
            max_concurrent_positions=raw.get("max_concurrent_positions"),
            trading_enabled_env_var=raw.get("trading_enabled_env_var", "TRADING_ENABLED"),
        )


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config

    def allow(
        self,
        *,
        open_position_count: int,
        realized_pnl_today_usd: float,
    ) -> tuple[bool, str | None]:
        cap = self.config.max_concurrent_positions
        if cap is not None and open_position_count >= cap:
            return False, f"max_concurrent_positions reached ({open_position_count}/{cap})"

        loss_cap = self.config.daily_loss_cap_usd
        if loss_cap is not None and realized_pnl_today_usd <= -abs(loss_cap):
            return False, f"daily_loss_cap reached (${realized_pnl_today_usd:.2f})"

        return True, None

    def stake_usd(self) -> float:
        return self.config.stake_usd
