"""Расписания: по времени, по диапазону, по закату/рассвету + догон пропущенного.

Идея из интеграции «Время света» (`time_of_light`): расписание по сенсору времени со смещением,
проверка результата, догон пропущенного события после простоя.

Типы запуска (`trigger`):
  time    — фиксированное время `time`;
  range   — диапазон `time_from`…`time_to`: в начале включаем (на `seconds`), в конце выключаем;
  sunset  — закат + `offset_min` (минуты, минус — раньше);
  sunrise — рассвет + `offset_min`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
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

    def __init__(
        self, hass: HomeAssistant, entry, settings, actions, coordinator
    ) -> None:
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
        self.hass.async_create_task(self._async_run_due(startup=True))

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now=None) -> None:
        await self._async_run_due()

    # ------------------------------------------------------------------ якоря
    def _sun_event(self, kind: str, day: date) -> datetime | None:
        """Время заката/рассвета на дату (локальное)."""
        try:
            from homeassistant.helpers.sun import get_astral_event_date

            value = get_astral_event_date(
                self.hass, "sunset" if kind == "sunset" else "sunrise", day
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("light_by_video: не удалось получить %s: %s", kind, err)
            return None
        if value is None:
            return None
        return dt_util.as_local(value) if value.tzinfo else value

    def _anchors(
        self, sched: dict[str, Any], now: datetime
    ) -> list[tuple[datetime, str]]:
        """Все точки срабатывания вокруг сегодняшнего дня: [(время, действие)]."""
        trigger = sched.get("trigger") or "time"
        days = sched.get("days") or []
        out: list[tuple[datetime, str]] = []
        for shift in (-1, 0, 1):
            day = (now + timedelta(days=shift)).date()
            if days and day.weekday() not in days:
                continue
            if trigger == "range":
                hf, mf = _parse_time(sched.get("time_from") or "00:00")
                ht, mt = _parse_time(sched.get("time_to") or "23:59")
                start = datetime.combine(day, dtime(hf, mf), tzinfo=now.tzinfo)
                end = datetime.combine(day, dtime(ht, mt), tzinfo=now.tzinfo)
                if end <= start:
                    end += timedelta(days=1)
                out.append((start, "on"))
                out.append((end, "off"))
            elif trigger in ("sunset", "sunrise"):
                when = self._sun_event(trigger, day)
                if when is not None:
                    offset = int(sched.get("offset_min") or 0)
                    out.append(
                        (when + timedelta(minutes=offset), sched.get("action") or "on")
                    )
            else:
                hh, mm = _parse_time(sched.get("time"))
                when = datetime.combine(day, dtime(hh, mm), tzinfo=now.tzinfo)
                out.append((when, sched.get("action") or "on"))
        return sorted(out)

    # ------------------------------------------------------------------ расчёт
    def next_run(self) -> dict[str, Any] | None:
        """Ближайшее будущее срабатывание с учётом типа запуска."""
        now = dt_util.now()
        best: tuple[datetime, dict, str] | None = None
        for sched in self.settings.schedules:
            if not sched.get("enabled"):
                continue
            for when, action in self._anchors(sched, now):
                if when <= now:
                    continue
                if best is None or when < best[0]:
                    best = (when, sched, action)
        if best is None:
            return None
        when, sched, action = best
        return {
            "at": when.isoformat(),
            "name": sched.get("name"),
            "action": action,
            "trigger": sched.get("trigger") or "time",
            "in_sec": int((when - now).total_seconds()),
        }

    # ------------------------------------------------------------------ срабатывание
    async def _async_run_due(self, startup: bool = False) -> None:
        now = dt_util.now()
        changed = False
        for sched in list(self.settings.schedules):
            if not sched.get("enabled"):
                continue
            last = self._parse_iso(sched.get("last_run"))
            due = [
                (when, action)
                for when, action in self._anchors(sched, now)
                if when <= now and (last is None or last < when)
            ]
            if not due:
                continue
            when, action = max(due)  # самое свежее из пропущенных
            late_min = (now - when).total_seconds() / 60.0
            # допуск не меньше минуты: тик 20 с, иначе при catchup=0 событие не успевало бы сработать
            limit = max(1.0, float(self.settings.catchup_min or 0))
            if late_min > limit:
                sched["last_run"] = when.isoformat()
                changed = True
                _LOGGER.info(
                    "light_by_video: расписание «%s» пропущено (%s, опоздание %.0f мин > %s)",
                    sched.get("name"),
                    when.strftime("%d.%m %H:%M"),
                    late_min,
                    self.settings.catchup_min,
                )
                continue
            await self._async_run(sched, when, late_min, action=action)
            changed = True
        if changed:
            await self.settings.async_save({"schedules": self.settings.schedules})

    async def _async_run(
        self,
        sched: dict[str, Any],
        when: datetime,
        late_min: float,
        action: str | None = None,
    ) -> None:
        zone = self.settings.zone(str(sched.get("zone_id") or "")) or next(
            (z for z in self.settings.zones if z.get("role") == "motion"), None
        )
        name = sched.get("name")
        act = action or sched.get("action") or "on"
        result: dict[str, Any] = {"ok": False, "reason": "нет зоны"}
        if zone is not None:
            if act == "off":
                entity = (zone.get("target_entity") or "").strip()
                if entity:
                    await self.actions.async_stop(entity, f"расписание «{name}»")
                    result = {"ok": True, "reason": "выключено"}
            else:
                target = {**zone, "hold_sec": int(sched.get("seconds") or 600)}
                result = await self.actions.async_trigger(
                    target, {"schedule": name, "late_min": round(late_min, 1)}
                )

        sched["last_run"] = when.isoformat()
        await self.settings.async_save({"schedules": self.settings.schedules})

        entry = {
            "ts": dt_util.utcnow().isoformat(),
            "event": EVENT_SCHEDULE,
            "title": EVENT_TITLES.get(EVENT_SCHEDULE, EVENT_SCHEDULE),
            "entry_id": self.entry.entry_id,
            "schedule": name,
            "trigger": sched.get("trigger") or "time",
            "action": act,
            "zone": zone.get("name") if zone else None,
            "late_min": round(late_min, 1),
            "ok": result.get("ok"),
            "reason": result.get("reason", ""),
        }
        self.hass.bus.async_fire(EVENT_BUS, entry)
        _LOGGER.info(
            "light_by_video: расписание «%s» (%s, %s): %s",
            name,
            sched.get("trigger") or "time",
            act,
            result.get("reason"),
        )

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        return dt_util.as_local(parsed) if parsed.tzinfo else parsed
