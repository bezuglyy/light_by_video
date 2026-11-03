"""Тумблеры: общие действия интеграции и действия отдельной зоны."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor import LightByVideoBase
from .const import DOMAIN
from .coordinator import LightByVideoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: LightByVideoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [ActionsSwitch(coordinator, entry)]
    for zone in coordinator.settings.zones:
        entities.append(ZoneActionsSwitch(coordinator, entry, zone["id"]))
    async_add_entities(entities)


class ActionsSwitch(LightByVideoBase, SwitchEntity):
    """Общий тумблер: включать ли устройства по движению."""

    _attr_translation_key = "actions"
    _attr_icon = "mdi:lightbulb-auto-outline"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_actions"

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("enabled", True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.settings.async_save({"enabled": True})
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.settings.async_save({"enabled": False})
        await self.coordinator.async_request_refresh()


class ZoneActionsSwitch(LightByVideoBase, SwitchEntity):
    """Тумблер действия одной зоны (не удаляя её настройки)."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:toggle-switch-outline"

    def __init__(self, coordinator: LightByVideoCoordinator, entry: ConfigEntry, zone_id: str) -> None:
        super().__init__(coordinator, entry)
        self._zone_id = zone_id
        zone = coordinator.settings.zone(zone_id) or {}
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone_id}_switch"
        self._attr_name = f"{zone.get('name') or zone_id} · действие"

    @property
    def _zone(self) -> dict[str, Any]:
        return self.coordinator.settings.zone(self._zone_id) or {}

    @property
    def is_on(self) -> bool:
        return bool(self._zone.get("enabled", True))

    async def _async_set(self, enabled: bool) -> None:
        zones = [dict(z) for z in self.coordinator.settings.zones]
        for zone in zones:
            if zone["id"] == self._zone_id:
                zone["enabled"] = enabled
        await self.coordinator.settings.async_save({"zones": zones})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
