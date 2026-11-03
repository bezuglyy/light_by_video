"""Расписания принудительного включения/выключения (с догоном пропущенного простоя).

Идея взята из интеграции «Время света» (`time_of_light`): срабатывание по времени, дни недели,
догон пропущенного события в пределах `catchup_min` (если HA был выключен), журнал и «следующее событие».
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import EVENT_BUS, EVENT_SCHEDULE, EVENT_TITLES

_LOGGER = logging.getLogger(__name__)

TICK_SEC = 20


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return 22, 0


class ScheduleRunner:
    """Тикер расписаний для одной записи интеграции."""

    def __init__(self, hass: HomeAssistant, entry, settings, actions, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = settings
        self.actions = actions
        self.coordinator = coordinator
        self._unsub = None

    # ------------------------------------------------------------------ жизнь
    def start(self) -> None:
        if self._unsub is not None:
            return
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=TICK_SEC)
        )
        # сразу после старта — догон пропущенного (HA мог быть выключен)
        self.hass.async_create_task(self._async_run_due(startup=True))

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now=None) -> None:
        await self._async_run_due()

    # ------------------------------------------------------------------ расчёт
    def next_run(self) -> dict[str, Any] | None:
        """Ближайшее будущее срабатывание (до 8 дней вперёд)."""
        now = dt_util.now()
        best: tuple[datetime, dict] | None = None
        for sched in self.settings.schedules:
            if not sched.get("enabled"):
                continue
            hh, mm = _parse_time(sched.get("time"))
            for shift in range(0, 8):
                day = (now + timedelta(days=shift)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if day.weekday() not in (sched.get("days") or []):
                    continue
                if day <= now:
                    continue
                if best is None or day < best[0]:
                    best = (day, sched)
                break
        if best is None:
            return None
        when, sched = best
        return {
            "at": when.isoformat(),
            "name": sched.get("name"),
            "action": sched.get("action"),
            "in_sec": int((when - now).total_seconds()),
        }

    # ------------------------------------------------------------------ срабатывание
    async def _async_run_due(self, startup: bool = False) -> None:
        now = dt_util.now()
        changed = False
        for sched in list(self.settings.schedules):
            if not sched.get("enabled"):
                continue
            hh, mm = _parse_time(sched.get("time"))
            last = self._parse_iso(sched.get("last_run"))
            # проверяем «сегодня» и, если сегодняшнее время ещё не наступило/не выполнялось, «вчера»
            candidates = [
                now.replace(hour=hh, minute=mm, second=0, microsecond=0),
                now.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=1),
            ]
            for when in candidates:
                if when > now:
                    continue
                if when.weekday() not in (sched.get("days") or []):
                    continue
                if last is not None and last >= when:
                    continue
                late_min = (now - when).total_seconds() / 60.0
                if late_min > float(self.settings.catchup_min or 0):
                    sched["last_run"] = when.isoformat()
                    changed = True
                    _LOGGER.info(
                        "light_by_video: расписание «%s» пропущено (опоздание %.0f мин > %s)",
                        sched.get("name"), late_min, self.settings.catchup_min,
                    )
                    break
                await self._async_run(sched, when, late_min)
                break
        if changed:
            await self.settings.async_save({"schedules": self.settings.schedules})

    async def _async_run(self, sched: dict[str, Any], when: datetime, late_min: float) -> None:
        zone = self.settings.zone(str(sched.get("zone_id") or "")) or next(
            (z for z in self.settings.zones if z.get("role") == "motion"), None
        )
        name = sched.get("name")
        result: dict[str, Any] = {"ok": False, "reason": "нет зоны"}
        if zone is not None:
            if sched.get("action") == "off":
                entity = (zone.get("target_entity") or "").strip()
                if entity:
                    await self.actions.async_stop(entity, f"расписание «{name}»")
                    result = {"ok": True, "reason": "выключено"}
            else:
                target = {**zone, "hold_sec": int(sched.get("seconds") or 600)}
                result = await self.actions.async_trigger(target, {"schedule": name, "late_min": round(late_min, 1)})

        sched["last_run"] = when.isoformat()
        await self.settings.async_save({"schedules": self.settings.schedules})

        entry = {
            "ts": dt_util.utcnow().isoformat(),
            "event": EVENT_SCHEDULE,
            "title": EVENT_TITLES.get(EVENT_SCHEDULE, EVENT_SCHEDULE),
            "entry_id": self.entry.entry_id,
            "schedule": name,
            "action": sched.get("action"),
            "zone": zone.get("name") if zone else None,
            "late_min": round(late_min, 1),
            "ok": result.get("ok"),
            "reason": result.get("reason", ""),
        }
        self.hass.bus.async_fire(EVENT_BUS, entry)
        _LOGGER.info("light_by_video: расписание «%s» (%s): %s", name, sched.get("action"), result.get("reason"))

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        return dt_util.as_local(parsed) if parsed.tzinfo else parsed
