"""HTTP API интеграции light_by_video (панель и внешние инструменты)."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CALIBRATION,
    CONF_CAMERA_ENTITY,
    CONF_DETECTOR,
    CONF_ENABLED,
    CONF_GO2RTC_BASE,
    CONF_INTERLOCKS,
    CONF_MUTE_UNTIL,
    CONF_NOTIFY,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_URL,
    CONF_SOURCE_PASS,
    CONF_SOURCE_USER,
    CONF_SUN_ENTITY,
    CONF_SUN_OFFSET,
    CONF_ZONES,
    DOMAIN,
    URL_EVENTS,
    URL_FRAME,
    URL_MASK,
    URL_SETTINGS,
    URL_STATE,
    URL_TEST,
    URL_ZONE,
)
from .coordinator import LightByVideoCoordinator
from .settings import Settings, normalize_zone

_LOGGER = logging.getLogger(__name__)


def _all(hass: HomeAssistant) -> dict[str, LightByVideoCoordinator]:
    out: dict[str, LightByVideoCoordinator] = {}
    for key, value in hass.data.get(DOMAIN, {}).items():
        if isinstance(value, LightByVideoCoordinator):
            out[str(key)] = value
    return out


def _entry_id(request: web.Request, hass: HomeAssistant) -> str | None:
    entry_id = request.query.get("entry_id")
    if entry_id:
        return entry_id
    items = _all(hass)
    return next(iter(items), None)


def _coordinator(
    hass: HomeAssistant, entry_id: str | None
) -> LightByVideoCoordinator | None:
    items = _all(hass)
    if entry_id and entry_id in items:
        return items[entry_id]
    return next(iter(items.values()), None)


def _settings(hass: HomeAssistant, entry_id: str | None) -> Settings | None:
    if entry_id:
        value = hass.data.get(DOMAIN, {}).get(f"settings:{entry_id}")
        if isinstance(value, Settings):
            return value
    for key, value in hass.data.get(DOMAIN, {}).items():
        if (
            isinstance(key, str)
            and key.startswith("settings:")
            and isinstance(value, Settings)
        ):
            return value
    return None


class LightByVideoFrameView(HomeAssistantView):
    """Последний кадр источника (для панели)."""

    url = URL_FRAME
    name = f"{DOMAIN}:frame"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        coordinator = _coordinator(hass, _entry_id(request, hass))
        if coordinator is None:
            return web.Response(status=404, text="light_by_video не настроен")
        if request.query.get("fresh"):
            try:
                await coordinator.async_request_refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: обновление кадра не удалось: %s", err)
        data = await coordinator.async_jpeg()
        if not data:
            return web.Response(status=503, text="кадр недоступен")
        return web.Response(
            body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )


class LightByVideoMaskView(HomeAssistantView):
    """Отладка: маска движения (PNG)."""

    url = URL_MASK
    name = f"{DOMAIN}:mask"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        coordinator = _coordinator(hass, _entry_id(request, hass))
        if coordinator is None:
            return web.Response(status=404, text="не настроено")
        png = await coordinator.async_mask_png()
        if not png:
            return web.Response(status=503, text="нет кадра")
        return web.Response(
            body=png, content_type="image/png", headers={"Cache-Control": "no-store"}
        )


class LightByVideoStateView(HomeAssistantView):
    """Текущее состояние, метрики зон, журнал."""

    url = URL_STATE
    name = f"{DOMAIN}:state"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry_id = _entry_id(request, hass)
        coordinator = _coordinator(hass, entry_id)
        settings = _settings(hass, entry_id)
        if coordinator is None or settings is None:
            return web.json_response({"error": "не настроено"}, status=404)
        if request.query.get("fresh"):
            try:
                await coordinator.async_request_refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: обновление не удалось: %s", err)
        data = coordinator.data or {}
        return web.json_response(
            {
                "entry_id": entry_id,
                "state": data.get("state"),
                "camera_ok": data.get("camera_ok"),
                "enabled": data.get("enabled"),
                "frame": data.get("frame"),
                "ir": data.get("ir"),
                "mean": data.get("mean"),
                "global_ratio": data.get("global_ratio"),
                "lighting": data.get("lighting"),
                "reason": data.get("reason"),
                "zone_states": data.get("zones") or [],
                "actions": data.get("actions") or coordinator.actions.active_info(),
                "zone_actions": data.get("zone_actions") or {},
                "objects": data.get("objects"),
                "detector_mode": data.get("detector_mode"),
                "last_motion": data.get("last_motion"),
                "motion_count": data.get("motion_count"),
                "source": data.get("source"),
                "error": coordinator.last_error or None,
                "settings": settings.as_dict(),
                "event_log": list(coordinator.event_log)[-60:],
            }
        )


class LightByVideoSettingsView(HomeAssistantView):
    """Чтение и запись настроек (зоны, детектор, включение)."""

    url = URL_SETTINGS
    name = f"{DOMAIN}:settings"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        settings = _settings(hass, _entry_id(request, hass))
        if settings is None:
            return web.json_response({"error": "не настроено"}, status=404)
        return web.json_response(settings.as_dict())

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry_id = _entry_id(request, hass)
        settings = _settings(hass, entry_id)
        coordinator = _coordinator(hass, entry_id)
        if settings is None:
            return web.json_response({"error": "не настроено"}, status=404)
        try:
            body: dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "некорректный JSON"}, status=400)

        patch = {
            key: body[key]
            for key in (
                CONF_ZONES,
                CONF_DETECTOR,
                CONF_ENABLED,
                CONF_SCAN_INTERVAL,
                CONF_NOTIFY,
                CONF_SNAPSHOT_URL,
                CONF_CAMERA_ENTITY,
                CONF_GO2RTC_BASE,
                CONF_SOURCE_USER,
                CONF_SOURCE_PASS,
                CONF_MUTE_UNTIL,
                CONF_INTERLOCKS,
                CONF_CALIBRATION,
                CONF_SUN_ENTITY,
                CONF_SUN_OFFSET,
            )
            if key in body
        }
        if body.get("apply_calibration"):
            cal = settings.calibration or {}
            px_per_m = float(cal.get("px_per_m") or 0)
            person_m = float(cal.get("person_m") or 1.7)
            frame_w = float(cal.get("frame_w") or 1920)
            frame_h = float(cal.get("frame_h") or 1080)
            if px_per_m <= 0:
                return web.json_response({"error": "калибровка не заполнена"}, status=400)
            person_px = px_per_m * person_m
            model_px = person_px * 640.0 / frame_w
            tiling = 1 if model_px >= 40 else 2
            min_h = round(max(0.005, min(0.05, (person_px / frame_h) * 0.25)), 3)
            detector = dict(settings.detector)
            detector.update({"objects_tiling": tiling, "objects_min_h": min_h, "objects_conf": 0.25})
            patch[CONF_DETECTOR] = detector
        if not patch:
            return web.json_response({"error": "нечего менять"}, status=400)

        await settings.async_save(patch)
        if coordinator is not None:
            coordinator.update_interval = coordinator.update_interval.__class__(
                seconds=max(1.0, settings.scan_interval)
            )
            if request.query.get("refresh"):
                try:
                    await coordinator.async_request_refresh()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("light_by_video: обновление после настроек: %s", err)
        return web.json_response({"ok": True, "settings": settings.as_dict()})


class LightByVideoZoneView(HomeAssistantView):
    """Быстрый патч одной зоны (редактор в панели)."""

    url = URL_ZONE
    name = f"{DOMAIN}:zone"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        settings = _settings(hass, _entry_id(request, hass))
        if settings is None:
            return web.json_response({"error": "не настроено"}, status=404)
        try:
            body: dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "некорректный JSON"}, status=400)

        zones = [dict(z) for z in settings.zones]
        action = str(body.get("action") or "save")

        if action == "delete":
            zone_id = str(body.get("id") or "")
            zones = [z for z in zones if z["id"] != zone_id]
        elif action == "add":
            new_zone = body.get("zone") if isinstance(body.get("zone"), dict) else {}
            used = {z["id"] for z in zones}
            index = 1
            while f"z{index}" in used:
                index += 1
            new_zone = {**new_zone, "id": f"z{index}"}
            zones.append(normalize_zone(new_zone, index))
        else:
            zone_id = str(body.get("id") or "")
            patch = body.get("zone") if isinstance(body.get("zone"), dict) else body
            for i, zone in enumerate(zones):
                if zone["id"] == zone_id:
                    zones[i] = normalize_zone({**zone, **patch, "id": zone_id}, i + 1)
                    break
            else:
                return web.json_response(
                    {"error": f"зона {zone_id} не найдена"}, status=404
                )

        await settings.async_save({CONF_ZONES: zones})
        return web.json_response({"ok": True, "zones": settings.as_dict()["zones"]})


class LightByVideoTestView(HomeAssistantView):
    """«Проверить сейчас» и тестовое включение устройства."""

    url = URL_TEST
    name = f"{DOMAIN}:test"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry_id = _entry_id(request, hass)
        coordinator = _coordinator(hass, entry_id)
        if coordinator is None:
            return web.json_response({"error": "не настроено"}, status=404)
        try:
            body: dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}

        if body.get("action") == "reset_cooldown":
            zone_id = str(body.get("zone_id") or "")
            coordinator.actions.reset_cooldown(zone_id)
            await coordinator.async_request_refresh()
            return web.json_response({"ok": True, "reset": zone_id})

        if body.get("action") in ("on", "off"):
            zone_id = str(body.get("zone_id") or "")
            zone = coordinator.settings.zone(zone_id) or (
                coordinator.settings.zones[0] if coordinator.settings.zones else None
            )
            if zone is None:
                return web.json_response({"error": "нет зон"}, status=404)
            if body["action"] == "on":
                result = await coordinator.actions.async_trigger(zone, {"test": True})
                return web.json_response(
                    {"ok": bool(result.get("ok")), "result": result}
                )
            await coordinator.actions.async_stop_all("тест из панели")
            return web.json_response({"ok": True})

        try:
            await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(
            {
                "ok": True,
                "state": coordinator.data or {},
                "actions": coordinator.actions.active_info(),
            }
        )


class LightByVideoEventsView(HomeAssistantView):
    """Журнал событий интеграции."""

    url = URL_EVENTS
    name = f"{DOMAIN}:events"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        coordinator = _coordinator(hass, _entry_id(request, hass))
        if coordinator is None:
            return web.json_response({"error": "не настроено"}, status=404)
        limit = int(request.query.get("limit", 50))
        return web.json_response({"events": list(coordinator.event_log)[-limit:]})
