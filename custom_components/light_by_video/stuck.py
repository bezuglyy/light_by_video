"""Сторож «движения нет, а устройство горит».

Смысл: интеграция держит свет только своим таймером (он в `ActionEngine._active`). Если устройство
включено, нашего таймера на нём нет и движения давно не было — это «горит без движения»: ручное
включение, залипание модуля, потерянная команда выключения. Сторож сообщает об этом и (по настройке)
выключает устройство.

Настройка `stuck` в конфиге записи:
  enabled    — включён ли сторож (по умолчанию да)
  grace_min  — сколько минут устройство должно гореть без нашего таймера, прежде чем реагировать (10)
  mode       — `notify` (только событие/журнал) или `off` (ещё и выключить)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from datetime import timedelta

from .const import EVENT_BUS, EVENT_STUCK, EVENT_TITLES

_LOGGER = logging.getLogger(__name__)

CHECK_SEC = 60
_ON_STATES = {"on", "open", "heat", "cool", "playing"}
_NOTIFY_EVERY_SEC = 900  # не чаще раза в 15 минут про одно и то же устройство


class StuckWatcher:
    """Проверка «горит, хотя движения нет»."""

    def __init__(
        self, hass: HomeAssistant, entry, settings, actions, coordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = settings
        self.actions = actions
        self.coordinator = coordinator
        self._unsub = None
        self._since: dict[str, float] = {}  # когда заметили «горит без нашего таймера»
        self._notified: dict[str, float] = {}
        self._last: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ жизнь
    def start(self) -> None:
        if self._unsub is not None:
            return
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=CHECK_SEC)
        )

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now=None) -> None:
        await self.async_check()

    # ------------------------------------------------------------------ проверка
    def _motion_age(self) -> float | None:
        """Сколько секунд назад было последнее движение (None — движения не было вовсе)."""
        raw = getattr(self.coordinator, "last_motion_ts", None)
        if not raw:
            return None
        try:
            when = dt_util.parse_datetime(str(raw))
        except (ValueError, TypeError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = dt_util.as_local(when)
        return max(0.0, (dt_util.utcnow() - when).total_seconds())

    def status(self) -> list[dict[str, Any]]:
        """Что сторож видит прямо сейчас."""
        return list(self._last)

    async def async_check(self, force: bool = False) -> list[dict[str, Any]]:
        cfg = self.settings.stuck or {}
        if not cfg.get("enabled", True):
            self._since.clear()
            self._last = []
            return []
        grace = float(cfg.get("grace_min") or 10) * 60.0
        mode = str(cfg.get("mode") or "notify")
        now = time.time()
        motion_age = self._motion_age()
        found: list[dict[str, Any]] = []

        active = getattr(self.actions, "_active", {}) or {}
        for zone in self.settings.zones:
            entity = str(zone.get("target_entity") or "").strip()
            if not entity:
                continue
            state = self.hass.states.get(entity)
            is_on = bool(state and str(state.state).lower() in _ON_STATES)
            if not is_on:
                self._since.pop(entity, None)
                continue
            if entity in active:
                # устройство держит наш таймер — это норма
                self._since.pop(entity, None)
                continue
            if motion_age is not None and motion_age < grace:
                self._since.pop(entity, None)
                continue
            first = self._since.get(entity)
            if first is None:
                self._since[entity] = now
                continue
            if not force and (now - first) < grace:
                continue

            item = {
                "entity": entity,
                "zone": zone.get("name"),
                "zone_id": zone.get("id"),
                "seconds": int(now - first),
                "motion_age": int(motion_age) if motion_age is not None else None,
                "state": str(state.state) if state else "",
            }
            found.append(item)

            last = self._notified.get(entity, 0.0)
            if force or (now - last) >= _NOTIFY_EVERY_SEC:
                self._notified[entity] = now
                await self._async_report(item, mode)
            if mode == "off":
                await self.actions.async_stop(entity, "горит без движения", force=True)
                self._since.pop(entity, None)
            else:
                self._since[entity] = now  # начинаем новый отсчёт, чтобы не спамить

        # устройства, которые больше не «залипли», убираем из уведомлений
        for entity in list(self._since):
            if entity not in {i["entity"] for i in found}:
                state = self.hass.states.get(entity)
                if (
                    state is None
                    or str(state.state).lower() not in _ON_STATES
                    or entity in active
                ):
                    self._since.pop(entity, None)

        self._last = found
        return found

    async def _async_report(self, item: dict[str, Any], mode: str) -> None:
        suffix = " — выключено" if mode == "off" else " — нужно выключить"
        entry = {
            "ts": dt_util.utcnow().isoformat(),
            "event": EVENT_STUCK,
            "title": EVENT_TITLES.get(EVENT_STUCK, EVENT_STUCK),
            "entry_id": self.entry.entry_id,
            "zone": item.get("zone"),
            "entity": item.get("entity"),
            "reason": f"движения нет {_human(item.get('motion_age'))}, устройство включено{suffix}",
            "seconds": item.get("seconds"),
            "mode": mode,
        }
        self.hass.bus.async_fire(EVENT_BUS, entry)
        _LOGGER.warning(
            "light_by_video: %s горит без движения (%s) — режим %s",
            item.get("entity"),
            item.get("seconds"),
            mode,
        )
        notify = self.settings.notify or {}
        service = notify.get("service") or ""
        if notify.get("enabled") and service:
            try:
                domain, name = (
                    service.split(".", 1) if "." in service else ("notify", service)
                )
                await self.hass.services.async_call(
                    domain,
                    name,
                    {
                        "title": entry["title"],
                        "message": f"{item.get('zone')}: {entry['reason']}",
                    },
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: notify сторожа не сработал: %s", err)


def _human(seconds: Any) -> str:
    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return "давно"
    if value < 60:
        return f"{value} с"
    minutes = value // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"
