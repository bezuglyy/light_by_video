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
from .schedules import ScheduleRunner
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
    runner = ScheduleRunner(hass, entry, settings, actions, coordinator)
    coordinator.schedule_runner = runner
    actions.schedule_runner = runner
    store[f"runner:{entry.entry_id}"] = runner

    await _async_register_http(hass)
    await _async_register_services(hass)
    await async_register_panel(hass)

    runner.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("light_by_video: интеграция запущена (%s)", entry.title)
    return True


def _pick_coordinator(hass: HomeAssistant, entry_id: str | None):
    store = hass.data.get(DOMAIN, {})
    if entry_id and isinstance(store.get(entry_id), LightByVideoCoordinator):
        return store[entry_id]
    return next((v for v in store.values() if isinstance(v, LightByVideoCoordinator)), None)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Сервисы интеграции: trigger / stop / reset_cooldown."""
    if hass.services.has_service(DOMAIN, "trigger"):
        return

    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    schema_trigger = vol.Schema(
        {
            vol.Optional("entry_id"): cv.string,
            vol.Optional("zone_id"): cv.string,
            vol.Optional("seconds"): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Optional("ignore_window"): cv.boolean,
        }
    )
    schema_stop = vol.Schema({vol.Optional("entry_id"): cv.string, vol.Optional("entity"): cv.string})
    schema_reset = vol.Schema({vol.Optional("entry_id"): cv.string, vol.Optional("zone_id"): cv.string})

    async def _trigger(call) -> None:
        coordinator = _pick_coordinator(hass, call.data.get("entry_id"))
        if coordinator is None:
            return
        zone_id = call.data.get("zone_id")
        zone = coordinator.settings.zone(str(zone_id)) if zone_id else next(
            (z for z in coordinator.settings.zones if z.get("role") == "motion"), None
        )
        if zone is None:
            return
        if call.data.get("seconds"):
            zone = {**zone, "hold_sec": int(call.data["seconds"])}
        await coordinator.actions.async_trigger(
            zone,
            {"service": "trigger"},
            respect_window=not bool(call.data.get("ignore_window")),
        )
        await coordinator.async_request_refresh()

    async def _stop(call) -> None:
        coordinator = _pick_coordinator(hass, call.data.get("entry_id"))
        if coordinator is None:
            return
        entity = call.data.get("entity")
        if entity:
            await coordinator.actions.async_stop(str(entity))
        else:
            await coordinator.actions.async_stop_all("сервис stop")
        await coordinator.async_request_refresh()

    async def _reset(call) -> None:
        coordinator = _pick_coordinator(hass, call.data.get("entry_id"))
        if coordinator is None:
            return
        zone_id = call.data.get("zone_id")
        if zone_id:
            coordinator.actions.reset_cooldown(str(zone_id))
        else:
            for zone in coordinator.settings.zones:
                coordinator.actions.reset_cooldown(zone["id"])
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "trigger", _trigger, schema=schema_trigger)
    hass.services.async_register(DOMAIN, "stop", _stop, schema=schema_stop)
    hass.services.async_register(DOMAIN, "reset_cooldown", _reset, schema=schema_reset)


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
