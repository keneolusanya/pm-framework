"""Day 5 Block 1: risk rails. Checked before every order, no exceptions.

Three guards:
  * per-trade size cap (start tiny: $10-25),
  * daily notional cap (sum of today's trade legs),
  * a kill switch — either a file (execute/KILL_SWITCH) or RISK_KILL_SWITCH=1 —
    that halts all execution the moment it's present/truthy.

All limits come from the environment so they can be tightened without a code
change. This module never places orders; it only says yes/no.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

MAX_POSITION_USD = float(os.environ.get("RISK_MAX_POSITION_USD", "25"))
DAILY_NOTIONAL_CAP_USD = float(os.environ.get("RISK_DAILY_CAP_USD", "200"))
KILL_FILE = Path(__file__).resolve().parent / "KILL_SWITCH"


class RiskError(Exception):
    """Raised when a proposed order violates a risk rail."""


def kill_switch_engaged() -> bool:
    if KILL_FILE.exists():
        return True
    return os.environ.get("RISK_KILL_SWITCH", "").lower() in ("1", "true", "yes")


def _today_notional(con) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(price * size), 0) FROM trades "
        "WHERE created_at >= ?",
        [datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)],
    ).fetchone()
    return float(row[0] or 0)


def check_trade(con, size_usd: float) -> None:
    """Raise RiskError if this order shouldn't be placed. Returns None if OK."""
    if kill_switch_engaged():
        raise RiskError("kill switch engaged — execution halted")
    if size_usd <= 0:
        raise RiskError("order size must be positive")
    if size_usd > MAX_POSITION_USD:
        raise RiskError(f"order ${size_usd:.2f} exceeds per-trade cap "
                        f"${MAX_POSITION_USD:.2f}")
    projected = _today_notional(con) + size_usd
    if projected > DAILY_NOTIONAL_CAP_USD:
        raise RiskError(f"would breach daily notional cap "
                        f"(${projected:.2f} > ${DAILY_NOTIONAL_CAP_USD:.2f})")
