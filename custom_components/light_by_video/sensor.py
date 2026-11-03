"""Сенсоры: последнее движение, счётчик за сутки, метрика изменения кадра."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .binary_sensor import LightByVideoBase
from .const import DOMAIN
from .coordinator import LightByVideoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: LightByVideoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StatusSensor(coordinator, entry),
            LastActionSensor(coordinator, entry),
            NextEventSensor(coordinator, entry),
            LastMotionSensor(coordinator, entry),
            MotionCountSensor(coordinator, entry),
            FrameChangeSensor(coordinator, entry),
        ]
    )


class LastMotionSensor(LightByVideoBase, SensorEntity):
    """Когда последний раз было движение (локальное время)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_motion"
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_motion"

    @property
    def native_value(self):
        value = self.data.get("last_motion")
        if not value:
            return None
        try:
            return dt_util.parse_datetime(value)
        except (ValueError, TypeError):
            return None


class MotionCountSensor(LightByVideoBase, SensorEntity):
    """Сколько раз срабатывало движение за сутки."""

    _attr_translation_key = "motion_count"
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "раз"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_motion_count"

    @property
    def native_value(self) -> int:
        return int(self.data.get("motion_count") or 0)


class FrameChangeSensor(LightByVideoBase, SensorEntity):
    """Диагностика: максимальная доля изменённых пикселей по зонам."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "frame_change"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:image-filter-center-focus"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_frame_change"

    @property
    def native_value(self) -> float:
        zones = self.data.get("zones") or []
        ratio = max((float(z.get("ratio") or 0) for z in zones), default=0.0)
        return round(ratio * 100, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "zones": [
                {
                    "id": z.get("id"),
                    "name": z.get("name"),
                    "ratio_pct": round(float(z.get("ratio") or 0) * 100, 2),
                    "need_pct": round(float(z.get("need") or 0) * 100, 2),
                    "changed_px": z.get("changed_px"),
                    "active": z.get("active"),
                }
                for z in (self.data.get("zones") or [])
            ],
            "global_ratio_pct": round(float(self.data.get("global_ratio") or 0) * 100, 2),
            "ir": self.data.get("ir"),
            "actions": self.data.get("actions"),
            "objects": self.data.get("objects"),
            "detector_mode": self.data.get("detector_mode"),
        }


class StatusSensor(LightByVideoBase, SensorEntity):
    """Текстовый статус: что делает интеграция прямо сейчас."""

    _attr_translation_key = "status"
    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        data = self.data
        if not data.get("camera_ok"):
            return "источник недоступен"
        settings = self.coordinator.settings
        until = getattr(settings, "mute_until", None)
        if until is not None and dt_util.utcnow() < until:
            return "не беспокоить"
        actions = data.get("zone_actions") or {}
        for zone_id, item in actions.items():
            if item.get("cooldown_left"):
                return f"пауза {item['cooldown_left']} с ({zone_id})"
            if item.get("active"):
                return f"включено, осталось {item.get('left_sec')} с ({zone_id})"
        return "ожидание движения"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "enabled": self.data.get("enabled"),
            "detector_mode": self.data.get("detector_mode"),
            "objects": (self.data.get("objects") or {}).get("count"),
            "zone_actions": self.data.get("zone_actions") or {},
        }


class LastActionSensor(LightByVideoBase, SensorEntity):
    """Когда и что делала интеграция (включение/выключение/пропуск)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_action"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_action"

    @property
    def native_value(self):
        entry = self.data.get("last_action") or {}
        ts = entry.get("ts")
        if not ts:
            return None
        try:
            return dt_util.parse_datetime(ts)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry = self.data.get("last_action") or {}
        return {
            "event": entry.get("event"),
            "title": entry.get("title"),
            "zone": entry.get("zone"),
            "entity": entry.get("entity"),
            "reason": entry.get("reason"),
        }


class NextEventSensor(LightByVideoBase, SensorEntity):
    """Когда свет погаснет автоматически (ближайшее выключение)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "next_event"
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_next_event"

    @property
    def native_value(self):
        item = self.data.get("next_event") or {}
        at = item.get("at")
        if not at:
            return None
        try:
            return dt_util.utc_from_timestamp(float(at))
        except (ValueError, TypeError, OSError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self.data.get("next_event") or {}
        return {"in_sec": item.get("in_sec"), "zone": item.get("entity")}
