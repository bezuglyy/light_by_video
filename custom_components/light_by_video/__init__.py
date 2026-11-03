"""light_by_video — «Свет по видео»: движение в области кадра включает устройство на время."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .actions import ActionEngine
from .const import DOMAIN
from .coordinator import LightByVideoCoordinator
from .panel import async_register_panel, async_unregister_panel
from .settings import get_settings
from .views import (
    LightByVideoEventsView,
    LightByVideoFrameView,
    LightByVideoMaskView,
    LightByVideoSettingsView,
    LightByVideoStateView,
    LightByVideoTestView,
    LightByVideoZoneView,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    settings = get_settings(hass, entry)

    actions = ActionEngine(hass, entry, settings)
    await actions.async_load()

    coordinator = LightByVideoCoordinator(hass, entry, settings, actions)
    await coordinator.async_start()
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        # источник может быть временно недоступен — поднимаем интеграцию,
        # сущности оживут при первом успешном кадре
        _LOGGER.warning("light_by_video: первый кадр не получен (%s) — продолжаю", err)
        await coordinator.async_refresh()

    store = hass.data.setdefault(DOMAIN, {})
    store[entry.entry_id] = coordinator
    store[f"actions:{entry.entry_id}"] = actions

    await _async_register_http(hass)
    await async_register_panel(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("light_by_video: интеграция запущена (%s)", entry.title)
    return True


async def _async_register_http(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_views_registered"):
        return
    for view in (
        LightByVideoFrameView(),
        LightByVideoMaskView(),
        LightByVideoStateView(),
        LightByVideoSettingsView(),
        LightByVideoZoneView(),
        LightByVideoTestView(),
        LightByVideoEventsView(),
    ):
        hass.http.register_view(view)
    hass.data[f"{DOMAIN}_views_registered"] = True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    actions: ActionEngine | None = hass.data[DOMAIN].pop(
        f"actions:{entry.entry_id}", None
    )
    if actions is not None:
        await actions.async_unload()

    coordinator: LightByVideoCoordinator | None = hass.data[DOMAIN].pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_shutdown()

    hass.data[DOMAIN].pop(f"settings:{entry.entry_id}", None)
    if not [
        k for k in hass.data[DOMAIN] if not str(k).startswith(("settings:", "actions:"))
    ]:
        await async_unregister_panel(hass)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Применить настройки: интервал, источник, набор сущностей зон."""
    settings = get_settings(hass, entry)
    settings.reload()
    coordinator: LightByVideoCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return
    coordinator.update_interval = timedelta(seconds=max(1.0, settings.scan_interval))
    if coordinator.source_signature() != coordinator.source_signature_applied:
        _LOGGER.info("light_by_video: источник изменён — перезапускаю поток")
        await coordinator.async_restart_source()
        return
    zone_entities = sorted(z["id"] for z in settings.zones if z.get("entity"))
    if zone_entities != coordinator.zone_entities_applied:
        _LOGGER.info(
            "light_by_video: сущности зон %s — перезагружаю запись", zone_entities
        )
        await hass.config_entries.async_reload(entry.entry_id)
