"""Движок действий: включить устройство на время, продлить, выключить.

Правила:
  * включаем только если зона и интеграция включены и выполнены условия
    (время/дни/освещённость);
  * держим ``hold_sec`` после последнего движения (``extend``), но не дольше
    ``max_on_sec``;
  * не выключаем то, что горело до нас (``restore=false``);
  * если человек сам переключил устройство — не трогаем его (карантин);
  * активные таймеры переживают перезапуск Home Assistant (Store).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    IS_OFF_STATES,
    ISH_ON_STATES,
    EVENT_ACTION_OFF,
    EVENT_ACTION_ON,
    EVENT_BUS,
    EVENT_SKIPPED,
    EVENT_TITLES,
    MANUAL_HOLD_SEC,
    MIN_ACTION_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_NO_STATE = ("unavailable", "unknown", "")
_ON_STATES = ("on", "open", "opening", "home", "playing")


class ActionEngine:
    """Включение/выключение целевых устройств по движению."""

    def __init__(self, hass: HomeAssistant, entry, settings) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = settings
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._active: dict[str, dict[str, Any]] = {}
        self._handles: dict[str, list] = {}
        self._last_cmd: dict[str, float] = {}
        self._manual_until: dict[str, float] = {}
        self._pending_off: list = []
        self._cooldown_until: dict[str, float] = {}

    # ------------------------------------------------------------------ жизнь
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        now = time.time()
        for entity, item in (data.get("active") or {}).items():
            try:
                until = float(item.get("until") or 0)
                max_until = float(item.get("max_until") or until)
            except (TypeError, ValueError):
                continue
            if until <= now and max_until <= now:
                # срок истёк, пока HA был выключен — гасим, если включали мы.
                # Делаем это с задержкой: при старте сущности устройства могут быть ещё не загружены.
                if not item.get("was_on"):
                    self._schedule_off(entity, 30, 3)
                continue
            self._active[entity] = {
                "zone_id": item.get("zone_id"),
                "zones": item.get("zones") or [item.get("zone_id")],
                "until": until,
                "max_until": max_until,
                "on_since": float(item.get("on_since") or now),
                "was_on": bool(item.get("was_on")),
            }
            self._rearm(entity)
        if self._active:
            _LOGGER.info("light_by_video: восстановлено активных действий: %s", len(self._active))

    def _schedule_off(self, entity: str, delay: int, attempts: int) -> None:
        """Выключить устройство с повторами (после перезапуска HA сущности могут быть ещё не готовы)."""

        @callback
        def _fire(_now):
            self.hass.async_create_task(self._async_off_with_retry(entity, attempts))

        self._pending_off.append(async_call_later(self.hass, delay, _fire))

    async def _async_off_with_retry(self, entity: str, attempts: int) -> None:
        state = self.hass.states.get(entity)
        if state is not None and state.state not in _ON_STATES:
            return
        ok = await self._async_set(entity, False, reason="истёк таймер (после перезапуска)")
        if not ok and attempts > 1:
            self._schedule_off(entity, 30, attempts - 1)

    async def async_unload(self) -> None:
        for cancel in self._pending_off:
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        self._pending_off.clear()
        for handles in self._handles.values():
            for cancel in handles:
                try:
                    cancel()
                except Exception:  # noqa: BLE001
                    pass
        self._handles.clear()
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save({"active": self._active})

    # ------------------------------------------------------------------ условия
    def _allowed(self, zone: dict[str, Any]) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "интеграция выключена"
        mute_until = getattr(self.settings, "mute_until", None)
        if mute_until is not None and dt_util.utcnow() < mute_until:
            return False, f"режим «не беспокоить» до {dt_util.as_local(mute_until):%d.%m %H:%M}"
        if not zone.get("enabled", True):
            return False, "зона выключена"
        entity = (zone.get("target_entity") or "").strip()
        if not entity:
            return False, "не задано устройство"
        state = self.hass.states.get(entity)
        if state is None:
            return False, f"устройство {entity} не найдено"
        if state.state in _NO_STATE:
            return False, f"устройство {entity} недоступно"

        days = zone.get("days") or []
        now = dt_util.now()
        if days and now.weekday() not in days:
            return False, "не тот день недели"
        if not _in_window(now, zone.get("time_from"), zone.get("time_to")):
            return False, "вне окна времени"

        sun_mode = str(zone.get("sun") or "any")
        if sun_mode in ("night", "day"):
            night = _is_night(self.hass, self.settings)
            if sun_mode == "night" and not night:
                return False, "до заката (светло)"
            if sun_mode == "day" and night:
                return False, "после заката (темно)"

        blocked, reason = _interlocks_block(self.hass, self.settings, zone)
        if blocked:
            return False, reason

        lux_sensor = (zone.get("lux_sensor") or "").strip()
        lux_below = float(zone.get("lux_below") or 0)
        if lux_sensor and lux_below > 0:
            lux_state = self.hass.states.get(lux_sensor)
            try:
                lux = float(lux_state.state) if lux_state else None
            except (TypeError, ValueError):
                lux = None
            if lux is not None and lux >= lux_below:
                return False, f"светло ({lux:.0f} lx ≥ {lux_below:.0f})"
        return True, ""

    # ------------------------------------------------------------------ запуск
    async def async_trigger(self, zone: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Движение в зоне: включить (если нужно) и/или продлить."""
        entity = (zone.get("target_entity") or "").strip()
        zone_id = zone.get("id")
        now = time.time()

        if not entity:
            return {"ok": False, "reason": "не задано устройство"}

        if entity in self._active:
            # общий таймер на устройство: несколько зон продлевают один и тот же свет,
            # удержание берём максимальное, лимит max_on не отодвигаем
            item = self._active[entity]
            hold = max(5, int(zone.get("hold_sec") or 300))
            item["until"] = max(float(item.get("until") or 0), now + hold)
            item["zone_id"] = zone_id
            zones = item.setdefault("zones", [])
            if zone_id not in zones:
                zones.append(zone_id)
            self._rearm(entity)
            await self._async_save()
            return {"ok": True, "reason": "продлено", "extended": True}

        allowed, reason = self._allowed(zone)
        if not allowed:
            await self._async_event(EVENT_SKIPPED, {"zone": zone.get("name"), "reason": reason, "entity": entity})
            return {"ok": False, "reason": reason, "skipped": True}

        if now - self._last_cmd.get(entity, 0) < MIN_ACTION_INTERVAL:
            return {"ok": False, "reason": "слишком часто", "skipped": True}

        cool_until = self._cooldown_until.get(zone_id, 0.0)
        if now < cool_until:
            reason = f"пауза между включениями (ещё {int(cool_until - now)} с)"
            await self._async_event(EVENT_SKIPPED, {"zone": zone.get("name"), "reason": reason, "entity": entity})
            return {"ok": False, "reason": reason, "skipped": True}

        state = self.hass.states.get(entity)
        was_on = bool(state and state.state in _ON_STATES)
        hold = max(5, int(zone.get("hold_sec") or 300))
        max_on = max(hold, int(zone.get("max_on_sec") or 1800))

        ok = await self._async_set(entity, True, zone=zone)
        if not ok:
            return {"ok": False, "reason": "команда не выполнена"}

        self._active[entity] = {
            "zone_id": zone_id,
            "zones": [zone_id],
            "until": now + hold,
            "max_until": now + max_on,
            "on_since": now,
            "was_on": was_on,
        }
        self._rearm(entity)
        await self._async_save()
        await self._async_event(
            EVENT_ACTION_ON,
            {"zone": zone.get("name"), "entity": entity, "hold": hold, "was_on": was_on, **(context or {})},
        )
        return {"ok": True, "reason": "включено", "hold": hold, "was_on": was_on}

    async def async_stop_all(self, reason: str = "выключено вручную") -> None:
        for entity in list(self._active):
            await self._async_turn_off(entity, reason)
        await self._async_save()

    def active_info(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        return {
            entity: {
                **item,
                "left_sec": max(0, int(item["until"] - now)),
                "max_left_sec": max(0, int(item["max_until"] - now)),
            }
            for entity, item in self._active.items()
        }

    # ------------------------------------------------------------------ таймеры
    def _rearm(self, entity: str) -> None:
        for cancel in self._handles.pop(entity, []):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        item = self._active.get(entity)
        if not item:
            return
        now = time.time()
        hold_left = max(0.0, item["until"] - now)
        max_left = max(0.0, item["max_until"] - now)

        @callback
        def _hold_done(_now):
            self.hass.async_create_task(self._async_hold_done(entity))

        @callback
        def _max_done(_now):
            self.hass.async_create_task(self._async_turn_off(entity, "истёк максимальный срок"))

        self._handles[entity] = [
            async_call_later(self.hass, hold_left, _hold_done),
            async_call_later(self.hass, max_left, _max_done),
        ]

    async def _async_hold_done(self, entity: str) -> None:
        item = self._active.get(entity)
        if not item:
            return
        if time.time() < item["until"]:
            return
        await self._async_turn_off(entity, "истекло время удержания")

    async def _async_turn_off(self, entity: str, reason: str) -> None:
        item = self._active.pop(entity, None)
        for cancel in self._handles.pop(entity, []):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        if item is None:
            return

        state = self.hass.states.get(entity)
        if item.get("was_on") and not self._restore_allowed(entity):
            await self._async_event(
                EVENT_SKIPPED, {"entity": entity, "reason": "горело до нас — не выключаем", "detail": reason}
            )
            await self._async_save()
            return

        if state is not None and state.state in _ON_STATES:
            changed_after = 0.0
            try:
                changed_after = state.last_changed.timestamp() - float(item.get("on_since") or 0)
            except Exception:  # noqa: BLE001
                changed_after = 0.0
            if changed_after > 3 and self.hass.states.get(entity) is not None:
                # кто-то переключал устройство после нашей команды — не трогаем
                self._manual_until[entity] = time.time() + MANUAL_HOLD_SEC
                await self._async_event(
                    EVENT_SKIPPED, {"entity": entity, "reason": "ручное вмешательство — не выключаем", "detail": reason}
                )
                await self._async_save()
                return

        if await self._async_set(entity, False, reason=reason):
            await self._async_event(EVENT_ACTION_OFF, {"entity": entity, "reason": reason})
            zone = next((z for z in self.settings.zones if z.get("id") == item.get("zone_id")), None)
            cooldown = int((zone or {}).get("cooldown_sec") or 0)
            if cooldown > 0:
                self._cooldown_until[str(item.get("zone_id"))] = time.time() + cooldown
        await self._async_save()

    def _restore_allowed(self, entity: str) -> bool:
        for zone in self.settings.zones:
            if (zone.get("target_entity") or "").strip() == entity:
                return bool(zone.get("restore"))
        return False

    # ------------------------------------------------------------------ HA
    async def _async_set(self, entity: str, turn_on: bool, *, zone: dict[str, Any] | None = None, reason: str = "") -> bool:
        domain = entity.split(".", 1)[0]
        service = "turn_on" if turn_on else "turn_off"
        data: dict[str, Any] = {"entity_id": entity}
        if zone is not None and turn_on and domain == "light":
            brightness = zone.get("brightness")
            try:
                if brightness:
                    data["brightness"] = int(brightness)
            except (TypeError, ValueError):
                pass
        try:
            if domain in ("script", "scene"):
                await self.hass.services.async_call(domain, "turn_on", data, blocking=False)
            else:
                await self.hass.services.async_call("homeassistant", service, data, blocking=False)
            self._last_cmd[entity] = time.time()
            _LOGGER.info("light_by_video: %s -> %s (%s)", entity, "on" if turn_on else "off", reason or "движение")
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("light_by_video: команда %s %s не выполнена: %s", entity, service, err)
            return False

    async def _async_event(self, event: str, payload: dict[str, Any]) -> None:
        entry = {
            "ts": dt_util.utcnow().isoformat(),
            "event": event,
            "title": EVENT_TITLES.get(event, event),
            "entry_id": self.entry.entry_id,
            **payload,
        }
        self.hass.bus.async_fire(EVENT_BUS, entry)
        notification = (self.settings.notify or {}).get("enabled") and event in (EVENT_ACTION_ON, EVENT_ACTION_OFF)
        if not notification:
            return
        service = (self.settings.notify or {}).get("service") or ""
        if service:
            try:
                domain, name = service.split(".", 1) if "." in service else ("notify", service)
                await self.hass.services.async_call(
                    domain,
                    name,
                    {"title": entry["title"], "message": _text(entry)},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: notify не сработал: %s", err)


def _text(entry: dict[str, Any]) -> str:
    parts = [str(entry.get("title") or entry.get("event"))]
    for key in ("zone", "entity", "reason", "detail"):
        if entry.get(key):
            parts.append(str(entry[key]))
    return " · ".join(parts)


def _in_window(now: datetime, start: Any, end: Any) -> bool:
    start = str(start or "").strip()
    end = str(end or "").strip()
    if not start or not end:
        return True
    try:
        sh, sm = (int(x) for x in start.split(":")[:2])
        eh, em = (int(x) for x in end.split(":")[:2])
    except (ValueError, AttributeError):
        return True
    minutes = now.hour * 60 + now.minute
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


def _is_night(hass, settings) -> bool:
    """Темно ли сейчас по выбранному сенсору (по умолчанию sun.sun с поправкой k закату)."""
    entity = getattr(settings, "sun_entity", "sun.sun") or "sun.sun"
    state = hass.states.get(entity)
    if state is None:
        return False
    if entity == "sun.sun":
        if state.state == "below_horizon":
            return True
        offset = timedelta(minutes=int(getattr(settings, "sun_offset_min", 0) or 0))
        try:
            next_set = dt_util.parse_datetime(state.attributes.get("next_setting"))
        except Exception:  # noqa: BLE001
            next_set = None
        if next_set is not None and dt_util.now() >= dt_util.as_local(next_set) + offset:
            return True
        return False
    return str(state.state).lower() in ISH_ON_STATES


def _interlock_state(hass, rule: dict) -> tuple[bool, str]:
    """Сработало ли условие правила. Возвращает (сработало, пояснение)."""
    entity = (rule.get("entity") or "").strip()
    if not entity:
        return False, "не задана сущность"
    state = hass.states.get(entity)
    if state is None:
        return False, "сущность не найдена"
    op = rule.get("op") or "is_on"
    attribute = (rule.get("attribute") or "").strip()
    raw = state.attributes.get(attribute) if attribute else state.state
    name = state.name or entity
    if op == "is_on":
        return str(state.state).lower() in ISH_ON_STATES, f"{name}: {state.state}"
    if op == "is_off":
        return str(state.state).lower() in IS_OFF_STATES, f"{name}: {state.state}"
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return False, f"{name}: нет числа"
    value = rule.get("value")
    if value is None:
        return False, f"{name}: не задано значение"
    hit = {
        "above": number > float(value),
        "below": number < float(value),
        "equal": abs(number - float(value)) < 1e-9,
        "not_equal": abs(number - float(value)) > 1e-9,
    }.get(op, False)
    return bool(hit), f"{name}: {number} {op} {value}"


def _interlocks_block(hass, settings, zone: dict) -> tuple[bool, str]:
    """Проверить запреты. Разрешено всё, что не в списке; режим warn только пишет в лог."""
    for rule in getattr(settings, "interlocks", []) or []:
        if not rule.get("enabled", True):
            continue
        hit, text = _interlock_state(hass, rule)
        if not hit:
            continue
        if rule.get("mode") == "warn":
            _LOGGER.info("light_by_video: предупреждение «%s» (%s)", rule.get("name"), text)
            continue
        return True, f"запрет «{rule.get('name')}» ({text})"
    return False, ""
