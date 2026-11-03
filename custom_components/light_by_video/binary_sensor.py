"""Бинарные сенсоры: движение по зоне и доступность источника кадра."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, VERSION
from .coordinator import LightByVideoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: LightByVideoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [SourceBinarySensor(coordinator, entry)]
    for zone in coordinator.settings.zones:
        if zone.get("entity"):
            entities.append(ZoneMotionBinarySensor(coordinator, entry, zone["id"]))
    async_add_entities(entities)


class LightByVideoBase(CoordinatorEntity[LightByVideoCoordinator]):
    """Общая часть сущностей интеграции."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
            "model": "Свет по видео",
            "sw_version": VERSION,
        }

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data or {}


class ZoneMotionBinarySensor(LightByVideoBase, BinarySensorEntity):
    """Движение в нарисованной области кадра."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_has_entity_name = False
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry, zone_id: str) -> None:
        super().__init__(coordinator, entry)
        self._zone_id = zone_id
        zone = self._zone or {}
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_id}"
        self._attr_name = zone.get("name") or f"Зона {zone_id}"

    @property
    def _zone(self) -> dict[str, Any]:
        return self.coordinator.settings.zone(self._zone_id) or {}

    @property
    def _zone_data(self) -> dict[str, Any]:
        for item in self.data.get("zones") or []:
            if item.get("id") == self._zone_id:
                return item
        return {}

    @property
    def is_on(self) -> bool:
        return bool(self._zone_data.get("active"))

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success) and bool(self.data.get("camera_ok", True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._zone_data
        zone = self._zone
        return {
            "zone_id": self._zone_id,
            "ratio": item.get("ratio"),
            "need": item.get("need"),
            "changed_px": item.get("changed_px"),
            "target_entity": zone.get("target_entity"),
            "hold_sec": zone.get("hold_sec"),
            "extend": zone.get("extend"),
            "geometry": {k: zone.get(k) for k in ("x", "y", "w", "h")},
            "role": zone.get("role"),
            "sensitivity": zone.get("sensitivity"),
        }


class SourceBinarySensor(LightByVideoBase, BinarySensorEntity):
    """Диагностика: источник кадра отвечает."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "source"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_source"

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("camera_ok"))

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source": self.data.get("source"),
            "error": self.coordinator.last_error or None,
            "frame": self.data.get("frame"),
            "ir": self.data.get("ir"),
        }
