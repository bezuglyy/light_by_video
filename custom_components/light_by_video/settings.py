"""Настройки интеграции light_by_video: нормализация, сохранение, кэш."""

from __future__ import annotations

import copy
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CAMERA_ENTITY,
    CONF_DETECTOR,
    CONF_ENABLED,
    CONF_GO2RTC_BASE,
    CONF_NOTIFY,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_URL,
    CONF_SOURCE_PASS,
    CONF_SOURCE_USER,
    CONF_ZONES,
    DAYS_ALL,
    DEFAULT_DETECTOR,
    DEFAULT_GO2RTC_BASE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_ZONE,
    DOMAIN,
    HOLD_MAX,
    HOLD_MIN,
    MAX_ON_MIN,
    MAX_ZONES,
    ROLE_MOTION,
    SUN_MODES,
    ZONE_ROLES,
)

_LOGGER = logging.getLogger(__name__)


def _num(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_zone(zone: dict[str, Any], index: int) -> dict[str, Any]:
    """Привести зону к корректному виду (доли 0..1, роли и числа из допустимых)."""
    role = str(zone.get("role") or ROLE_MOTION)
    if role not in ZONE_ROLES:
        role = ROLE_MOTION

    x = _num(zone.get("x"), 0.25, 0.0, 1.0)
    y = _num(zone.get("y"), 0.25, 0.0, 1.0)
    w = _num(zone.get("w"), 0.3, 0.005, 1.0)
    h = _num(zone.get("h"), 0.4, 0.005, 1.0)
    if x + w > 1.0:
        w = max(0.005, 1.0 - x)
    if y + h > 1.0:
        h = max(0.005, 1.0 - y)

    hold = int(_num(zone.get("hold_sec"), 300, HOLD_MIN, HOLD_MAX))
    max_on = int(_num(zone.get("max_on_sec"), 1800, hold, 86400))
    cooldown = int(_num(zone.get("cooldown_sec"), 0, 0, 86400))
    days = zone.get("days")
    if not isinstance(days, list) or not days:
        days = list(DAYS_ALL)
    days = sorted({int(d) for d in days if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})

    return {
        "id": str(zone.get("id") or f"z{index}"),
        "name": str(zone.get("name") or f"Зона {index}"),
        "role": role,
        "x": round(x, 4),
        "y": round(y, 4),
        "w": round(w, 4),
        "h": round(h, 4),
        "enabled": bool(zone.get("enabled", True)),
        "sensitivity": round(_num(zone.get("sensitivity"), 1.0, 0.2, 5.0), 2),
        "target_entity": str(zone.get("target_entity") or "").strip(),
        "hold_sec": hold,
        "cooldown_sec": cooldown,
        "extend": bool(zone.get("extend", True)),
        "max_on_sec": max_on,
        "restore": bool(zone.get("restore", False)),
        "sun": _sun_mode(zone.get("sun")),
        "lux_sensor": str(zone.get("lux_sensor") or "").strip(),
        "lux_below": round(_num(zone.get("lux_below"), 0.0, 0.0, 100000.0), 1),
        "time_from": _hhmm(zone.get("time_from")),
        "time_to": _hhmm(zone.get("time_to")),
        "days": days or list(DAYS_ALL),
        "entity": bool(zone.get("entity", True)),
    }


def _sun_mode(value: Any) -> str:
    """Условие по солнцу: any | night | day."""
    text = str(value or "").strip().lower()
    return text if text in SUN_MODES else "any"


def _hhmm(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        hh, mm = text.split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return ""


def normalize_detector(detector: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_DETECTOR)
    for key, value in (detector or {}).items():
        if key not in out:
            continue
        if key in ("mode", "objects_url", "objects_classes"):
            out[key] = str(value)
            continue
        if key == "objects_require_motion":
            out[key] = bool(value)
            continue
        try:
            if key in ("width", "blur", "min_px", "confirm_frames", "confirm_window", "quiet_frames", "objects_cache_ms"):
                out[key] = int(value)
            else:
                out[key] = float(value)
        except (TypeError, ValueError):
            continue
    out["width"] = max(160, min(1280, int(out["width"])))
    out["blur"] = max(0, min(3, int(out["blur"])))
    out["min_px"] = max(5, int(out["min_px"]))
    out["confirm_frames"] = max(1, min(10, int(out["confirm_frames"])))
    out["confirm_window"] = max(out["confirm_frames"], min(20, int(out["confirm_window"])))
    out["quiet_frames"] = max(1, min(60, int(out["quiet_frames"])))
    mode = str(out.get("mode") or "frame")
    out["mode"] = mode if mode in ("frame", "objects") else "frame"
    out["objects_url"] = str(out.get("objects_url") or "").strip()
    out["objects_classes"] = str(out.get("objects_classes") or "0,1,2,3,5,7").strip()
    out["objects_conf"] = max(0.05, min(0.99, float(out.get("objects_conf") or 0.35)))
    out["objects_cache_ms"] = max(200, min(10000, int(out.get("objects_cache_ms") or 700)))
    out["objects_timeout"] = max(0.5, min(15.0, float(out.get("objects_timeout") or 4.0)))
    return out


class Settings:
    """Актуальные настройки записи (кэш в памяти для «горячих» правок из панели)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.reload()

    def reload(self) -> None:
        merged = {**self.entry.data, **self.entry.options}
        self.raw = merged

        self.camera_entity = str(merged.get(CONF_CAMERA_ENTITY) or "")
        self.snapshot_url = str(merged.get(CONF_SNAPSHOT_URL) or "")
        self.go2rtc_base = str(merged.get(CONF_GO2RTC_BASE) or DEFAULT_GO2RTC_BASE)
        self.source_user = str(merged.get(CONF_SOURCE_USER) or "")
        self.source_pass = str(merged.get(CONF_SOURCE_PASS) or "")
        self.scan_interval = _num(merged.get(CONF_SCAN_INTERVAL), DEFAULT_SCAN_INTERVAL, 1.0, 30.0)
        self.enabled = bool(merged.get(CONF_ENABLED, True))
        self.detector = normalize_detector(merged.get(CONF_DETECTOR))

        zones = merged.get(CONF_ZONES)
        if not isinstance(zones, list) or not zones:
            zones = [dict(DEFAULT_ZONE)]
        self.zones: list[dict[str, Any]] = [
            normalize_zone(zone, i + 1) for i, zone in enumerate(zones[:MAX_ZONES]) if isinstance(zone, dict)
        ]
        if not self.zones:
            self.zones = [normalize_zone(dict(DEFAULT_ZONE), 1)]

        notify = merged.get(CONF_NOTIFY)
        self.notify: dict[str, Any] = {
            "enabled": bool((notify or {}).get("enabled")),
            "service": str((notify or {}).get("service") or ""),
            "webhook_url": str((notify or {}).get("webhook_url") or ""),
        }

    # ------------------------------------------------------------------ зоны
    def zones_by_role(self, role: str) -> list[dict[str, Any]]:
        return [z for z in self.zones if z.get("role") == role]

    def zone(self, zone_id: str) -> dict[str, Any] | None:
        return next((z for z in self.zones if z["id"] == zone_id), None)

    # ------------------------------------------------------------------ сохранение
    async def async_save(self, patch: dict[str, Any]) -> None:
        options = dict(self.entry.options)
        for key, value in patch.items():
            if key == CONF_ZONES and isinstance(value, list):
                options[CONF_ZONES] = [normalize_zone(z, i + 1) for i, z in enumerate(value) if isinstance(z, dict)]
            elif key == CONF_DETECTOR and isinstance(value, dict):
                options[CONF_DETECTOR] = normalize_detector({**self.detector, **value})
            elif key == CONF_NOTIFY and isinstance(value, dict):
                options[CONF_NOTIFY] = {**self.notify, **value}
            elif key in (CONF_CAMERA_ENTITY, CONF_SNAPSHOT_URL, CONF_GO2RTC_BASE, CONF_SOURCE_USER, CONF_SOURCE_PASS):
                options[key] = str(value or "")
            elif key == CONF_ENABLED:
                options[key] = bool(value)
            elif key == CONF_SCAN_INTERVAL:
                options[key] = _num(value, DEFAULT_SCAN_INTERVAL, 1.0, 30.0)
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.reload()

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_entity": self.camera_entity,
            "snapshot_url": self.snapshot_url,
            "go2rtc_base": self.go2rtc_base,
            "source_user": self.source_user,
            "source_pass": self.source_pass,
            "scan_interval": self.scan_interval,
            "enabled": self.enabled,
            "detector": self.detector,
            "zones": copy.deepcopy(self.zones),
            "notify": dict(self.notify),
        }


def get_settings(hass: HomeAssistant, entry: ConfigEntry) -> Settings:
    store = hass.data.setdefault(DOMAIN, {})
    settings = store.get(f"settings:{entry.entry_id}")
    if settings is None:
        settings = Settings(hass, entry)
        store[f"settings:{entry.entry_id}"] = settings
    return settings
