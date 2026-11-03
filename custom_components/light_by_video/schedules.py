"""Периоды работы (расписания): в каких окнах времени разрешены действия.

Важно: расписание — это **период**, а не разовое включение. Внутри периода работает обычная логика:
обнаружение человека в зоне → устройство включается на «держать» и затем выключается. Вне периода
действия не выполняются (событие «вне периода» пишется в журнал с троттлингом).

Типы периода (`trigger`):
  time    — с `time_from` до `time_to` (переход через полночь поддержан);
  sunset  — с заката (+`offset_min`) до рассвета следующего дня (+`offset_end_min`);
  sunrise — с рассвета (+`offset_min`) до заката (+`offset_end_min`).

Дни недели (`days`) относятся к дню **начала** периода.
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
SUN_CACHE_LIMIT = 64


def _parse_time(value: Any, default: tuple[int, int] = (19, 0)) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return default


class ScheduleRunner:
    """Периоды работы для одной записи интеграции."""

    def __init__(
        self, hass: HomeAssistant, entry, settings, actions, coordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = settings
        self.actions = actions
        self.coordinator = coordinator
        self._unsub = None
        self._sun_cache: dict[tuple[str, date], datetime | None] = {}
        self._prev_active: dict[str, bool] = {}
        self._computed_at = 0.0
        self._state: dict[str, Any] = {
            "active": True,
            "items": [],
            "until": None,
            "schedule": None,
        }
        self._skip_ts: dict[str, float] = {}
        self._first_tick = True

    # ------------------------------------------------------------------ жизнь
    def start(self) -> None:
        if self._unsub is not None:
            return
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=TICK_SEC)
        )
        self.hass.async_create_task(self._async_tick())

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now=None) -> None:
        state = self.window_state(force=True)
        if self._first_tick:
            # при старте HA просто запоминаем состояние, без событий «период начался»
            self._first_tick = False
            for sched in self.settings.schedules:
                sid = str(sched.get("id"))
                self._prev_active[sid] = any(i["id"] == sid and i["active"] for i in state["items"])
            return
        # закрытие периода: принудительно гасим то, что интеграция включила
        for sched in self.settings.schedules:
            sid = str(sched.get("id"))
            was = self._prev_active.get(sid, False)
            now_active = any(
                item["id"] == sid and item["active"] for item in state["items"]
            )
            if was and not now_active:
                await self._async_close_period(sched, state)
            elif not was and now_active and state["items"]:
                self._bus("start", sched, "период начался")
            self._prev_active[sid] = now_active

    async def _async_close_period(
        self, sched: dict[str, Any], state: dict[str, Any]
    ) -> None:
        name = sched.get("name")
        stopped: list[str] = []
        for zone_id in self._zone_ids(sched) or [
            z.get("id") for z in self.settings.zones
        ]:
            zone = self.settings.zone(str(zone_id))
            entity = (zone or {}).get("target_entity") or ""
            if not entity:
                continue
            if entity in getattr(self.actions, "_active", {}):
                await self.actions.async_stop(entity, f"конец периода «{name}»")
                stopped.append(entity)
        self._bus(
            "end",
            sched,
            "период закончился" + (f", выключено: {len(stopped)}" if stopped else ""),
        )
        _LOGGER.info(
            "light_by_video: период «%s» закончился (выключено %s)", name, len(stopped)
        )

    def _bus(self, kind: str, sched: dict[str, Any], reason: str) -> None:
        self.hass.bus.async_fire(
            EVENT_BUS,
            {
                "ts": dt_util.utcnow().isoformat(),
                "event": EVENT_SCHEDULE,
                "title": EVENT_TITLES.get(EVENT_SCHEDULE, EVENT_SCHEDULE),
                "entry_id": self.entry.entry_id,
                "schedule": sched.get("name"),
                "trigger": sched.get("trigger"),
                "action": kind,  # start | end
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------ периоды
    def _sun(self, kind: str, day: date) -> datetime | None:
        key = (kind, day)
        if key in self._sun_cache:
            return self._sun_cache[key]
        value = None
        try:
            from homeassistant.helpers.sun import get_astral_event_date

            raw = get_astral_event_date(
                self.hass, "sunset" if kind == "sunset" else "sunrise", day
            )
            if raw is not None:
                value = dt_util.as_local(raw) if raw.tzinfo else raw
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "light_by_video: не удалось получить %s на %s: %s", kind, day, err
            )
        if len(self._sun_cache) > SUN_CACHE_LIMIT:
            self._sun_cache.clear()
        self._sun_cache[key] = value
        return value

    def _window(
        self, sched: dict[str, Any], day: date, tz
    ) -> tuple[datetime, datetime] | None:
        """Окно периода, начинающегося в указанный день."""
        days = sched.get("days") or []
        if days and day.weekday() not in days:
            return None
        trigger = sched.get("trigger") or "time"
        start_off = timedelta(minutes=int(sched.get("offset_min") or 0))
        end_off = timedelta(minutes=int(sched.get("offset_end_min") or 0))

        if trigger == "time":
            hf, mf = _parse_time(sched.get("time_from"), (19, 0))
            ht, mt = _parse_time(sched.get("time_to"), (7, 0))
            start = datetime.combine(day, dtime(hf, mf), tzinfo=tz)
            end = datetime.combine(day, dtime(ht, mt), tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)
            return start, end

        if trigger == "sunset":
            sunset = self._sun("sunset", day)
            sunrise = self._sun("sunrise", day + timedelta(days=1))
            if sunset is None or sunrise is None:
                return None
            return sunset + start_off, sunrise + end_off

        # sunrise → закат того же дня
        sunrise = self._sun("sunrise", day)
        sunset = self._sun("sunset", day)
        if sunrise is None or sunset is None:
            return None
        return sunrise + start_off, sunset + end_off

    def _current_window(
        self, sched: dict[str, Any], now: datetime
    ) -> tuple[datetime, datetime] | None:
        tz = now.tzinfo
        for shift in (-1, 0):
            window = self._window(sched, (now + timedelta(days=shift)).date(), tz)
            if window is None:
                continue
            start, end = window
            if start <= now < end:
                return window
        return None

    def _zone_ids(self, sched: dict[str, Any]) -> list[str]:
        zone_id = str(sched.get("zone_id") or "").strip()
        return [zone_id] if zone_id else []

    # ------------------------------------------------------------------ проверки
    def is_allowed(self, zone_id: str) -> tuple[bool, str, str | None]:
        """Разрешены ли действия в зоне прямо сейчас. (разрешено, причина, имя периода)."""
        now = dt_util.now()
        related = [
            s
            for s in self.settings.schedules
            if s.get("enabled", True)
            and (not s.get("zone_id") or str(s.get("zone_id")) == str(zone_id))
        ]
        if not related:
            return True, "", None
        for sched in related:
            if self._current_window(sched, now) is not None:
                return True, "", str(sched.get("name") or "")
        return False, "вне периода работы", None

    def hold_override(self, zone_id: str) -> int:
        """Своё время удержания из активного периода (0 — брать из зоны)."""
        now = dt_util.now()
        for sched in self.settings.schedules:
            if not sched.get("enabled", True):
                continue
            if sched.get("zone_id") and str(sched.get("zone_id")) != str(zone_id):
                continue
            if int(sched.get("seconds") or 0) <= 0:
                continue
            if self._current_window(sched, now) is not None:
                return int(sched.get("seconds") or 0)
        return 0

    def skip_allowed_log(self, key: str, every: float = 60.0) -> bool:
        """Троттлинг журнала «вне периода» (не чаще раза в минуту на зону)."""
        import time as _time

        now = _time.time()
        if now - self._skip_ts.get(key, 0.0) < every:
            return False
        self._skip_ts[key] = now
        return True

    # ------------------------------------------------------------------ состояние
    def window_state(self, force: bool = False) -> dict[str, Any]:
        import time as _time

        now = dt_util.now()
        signature = tuple(
            (s.get("id"), bool(s.get("enabled", True)), s.get("trigger"), s.get("time_from"), s.get("time_to"),
             s.get("offset_min"), s.get("offset_end_min"), tuple(s.get("days") or ()), s.get("zone_id"))
            for s in self.settings.schedules
        )
        if not force and self._state and self._signature == signature and (_time.time() - self._computed_at) < 8:
            return self._state
        self._signature = signature
        items: list[dict[str, Any]] = []
        active_items: list[dict[str, Any]] = []
        for sched in self.settings.schedules:
            if not sched.get("enabled", True):
                continue
            window = self._current_window(sched, now)
            start = end = None
            if window is not None:
                start, end = window
            elif sched.get("trigger") == "time":
                # для отображения — ближайшее окно
                for shift in (0, 1, 2):
                    candidate = self._window(
                        sched, (now + timedelta(days=shift)).date(), now.tzinfo
                    )
                    if candidate and candidate[1] > now:
                        start, end = candidate
                        break
            elif sched.get("trigger") in ("sunset", "sunrise"):
                for shift in (0, 1):
                    candidate = self._window(
                        sched, (now + timedelta(days=shift)).date(), now.tzinfo
                    )
                    if candidate and candidate[1] > now:
                        start, end = candidate
                        break
            item = {
                "id": str(sched.get("id")),
                "name": sched.get("name"),
                "trigger": sched.get("trigger") or "time",
                "zone_id": sched.get("zone_id") or "",
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "active": window is not None,
            }
            items.append(item)
            if window is not None:
                active_items.append(item)

        self._computed_at = _time.time()
        self._state = {
            "active": bool(active_items) if items else True,
            "items": items,
            "until": max((i["end"] for i in active_items if i["end"]), default=None),
            "schedule": ", ".join(str(i["name"]) for i in active_items) or None,
        }
        return self._state

    def next_start(self) -> dict[str, Any] | None:
        """Ближайшее начало периода."""
        now = dt_util.now()
        best: tuple[datetime, dict, datetime] | None = None
        for sched in self.settings.schedules:
            if not sched.get("enabled", True):
                continue
            for shift in (-1, 0, 1, 2):
                window = self._window(
                    sched, (now + timedelta(days=shift)).date(), now.tzinfo
                )
                if window is None:
                    continue
                start, end = window
                if start <= now:
                    continue
                if best is None or start < best[0]:
                    best = (start, sched, end)
        if best is None:
            return None
        start, sched, end = best
        return {
            "at": start.isoformat(),
            "end": end.isoformat(),
            "name": sched.get("name"),
            "trigger": sched.get("trigger") or "time",
            "zone_id": sched.get("zone_id") or "",
            "in_sec": int((start - now).total_seconds()),
        }
