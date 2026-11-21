"""Activity heartbeats and activity slots derived from the sleep plan.

Every companion plugin publishes what it is doing right now, and reads what
the others are doing, so the persona's afternoons are not three plugins firing
at the same minute. Slots come from the awake window the body publishes
(wake-up to bedtime), not from fixed clock times.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

BODY_PLUGIN = "astrbot_plugin_body_needs"
COMPANIONS = (
    "astrbot_plugin_bili_whale",
    "astrbot_plugin_music_card",
    "astrbot_plugin_kongjian",
)


def _data_root() -> Path | None:
    """Return the plugin data root, or None when it cannot be resolved."""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path())
    except Exception:
        return None


def roll(*parts: str) -> float:
    """Return a stable pseudo-random float in [0, 1) for the given parts."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def write(
    plugin_name: str,
    kind: str,
    persona: str,
    until: float,
    focus: float = 0.6,
) -> None:
    """Publish a heartbeat while one plugin is doing something engaging."""
    root = _data_root()
    if root is None:
        return
    now = time.time()
    payload = {
        "active": until > now,
        "kind": kind,
        "persona": persona,
        "since": now,
        "until": max(now, float(until)),
        "focus": float(focus),
    }
    try:
        path = root / plugin_name / "activity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        temp.replace(path)
    except OSError:
        pass


def clear(plugin_name: str) -> None:
    """Mark the plugin as idle again."""
    write(plugin_name, "", "", 0.0)


def busy(plugin_name: str, now: float | None = None) -> tuple[float, str]:
    """Return the longest running activity from the other companions.

    Args:
        plugin_name: The caller, so it does not block on itself.
        now: Unix timestamp, defaults to the current time.

    Returns:
        (until, kind) or (0, "") when everybody else is idle.
    """
    root = _data_root()
    if root is None:
        return 0.0, ""
    stamp = time.time() if now is None else now
    until = 0.0
    kind = ""
    for name in COMPANIONS:
        if name == plugin_name:
            continue
        try:
            data = json.loads(
                (root / name / "activity.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("active"):
            continue
        try:
            end = float(data.get("until") or 0.0)
        except (TypeError, ValueError):
            continue
        if end > stamp and end > until:
            until = end
            kind = str(data.get("kind") or name)
    return until, kind


def awake_window(plan: dict[str, Any], now: float) -> tuple[float, float]:
    """Return (start, end) for the awake window of the current day.

    When the plan is missing, fall back to a plausible ten-hour day so the
    plugins still do something instead of freezing.
    """
    start = float(plan.get("awake_since") or 0.0)
    if plan.get("asleep"):
        start = max(start, float(plan.get("wake_at") or 0.0))
    if start <= 0:
        start = now - 2 * 3600
    end = float(plan.get("bedtime") or 0.0)
    if end <= start:
        end = start + 10 * 3600
    return start, end


def slots(
    plan: dict[str, Any],
    persona: str,
    day: str,
    kind: str,
    count: int,
    now: float,
) -> list[float]:
    """Pick deterministic activity slots inside the awake window.

    Args:
        plan: Sleep plan from the body plugin.
        persona: Persona used as the stable seed.
        day: Local date string, so each day differs.
        kind: Plugin kind, so two plugins rarely pick the same minute.
        count: How many slots to spread across the day.
        now: Current unix time.

    Returns:
        Sorted slot timestamps, skipping the nap window when possible.
    """
    start, end = awake_window(plan, now)
    lo = start + 2400.0
    hi = max(lo + 1800.0, end - 5400.0)
    span = (hi - lo) / max(1, count)
    picked: list[float] = []
    for index in range(count):
        base = lo + span * index
        offset = roll(persona, day, kind, str(index)) * span * 0.85
        picked.append(base + offset)
    nap = float(plan.get("nap_at") or 0.0)
    if nap:
        kept = [stamp for stamp in picked if not (nap - 600 <= stamp <= nap + 3600)]
        picked = kept or picked
    return sorted(picked)


def latest_slot(
    plan: dict[str, Any],
    persona: str,
    day: str,
    kind: str,
    count: int,
    now: float,
) -> float:
    """Return the first slot that is due now or still ahead."""
    for stamp in slots(plan, persona, day, kind, count, now):
        if stamp >= now - 1200:
            return stamp
    return 0.0
