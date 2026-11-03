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
