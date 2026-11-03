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
    CONF_INTERLOCKS,
    CONF_MUTE_UNTIL,
    CONF_NOTIFY,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_URL,
    CONF_SOURCE_PASS,
    CONF_SUN_ENTITY,
    CONF_SUN_OFFSET,
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
    INTERLOCK_MODES,
    INTERLOCK_OPS,
    MAX_INTERLOCKS,
    MAX_ON_MIN,
    MAX_ZONES,
    ZONE_MAX_POINTS,
    ZONE_MIN_POINTS,
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

    points = _normalize_points(zone.get("points"))
    if points:
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        x, y = min(xs), min(ys)
        w, h = max(0.005, max(xs) - x), max(0.005, max(ys) - y)

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
        "points": points,
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


def normalize_interlock(rule: dict[str, Any], index: int) -> dict[str, Any]:
    """Правило запрета: сущность + условие + режим (запрещать/предупреждать)."""
    op = str(rule.get("op") or "is_on")
    if op not in INTERLOCK_OPS:
        op = "is_on"
    mode = str(rule.get("mode") or "block")
    if mode not in INTERLOCK_MODES:
        mode = "block"
    value = rule.get("value")
    try:
        fvalue = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        fvalue = None
    return {
        "id": str(rule.get("id") or f"i{index}"),
        "name": str(rule.get("name") or f"Запрет {index}"),
        "entity": str(rule.get("entity") or "").strip(),
        "attribute": str(rule.get("attribute") or "").strip(),
        "op": op,
        "value": fvalue,
        "mode": mode,
        "enabled": bool(rule.get("enabled", True)),
    }


def _parse_dt(value: str):
    """ISO-строка -> aware datetime (или None)."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        from homeassistant.util import dt as dt_util

        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)
    except Exception:  # noqa: BLE001
        return None


def _normalize_points(value: Any) -> list[list[float]] | None:
    """Вершины многоугольника: список [x, y] в долях кадра (0..1). None — обычный прямоугольник."""
    if not isinstance(value, list) or len(value) < ZONE_MIN_POINTS:
        return None
    points: list[list[float]] = []
    for item in value[:ZONE_MAX_POINTS]:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                px = min(1.0, max(0.0, float(item[0])))
                py = min(1.0, max(0.0, float(item[1])))
            except (TypeError, ValueError):
                continue
            points.append([round(px, 4), round(py, 4)])
    return points if len(points) >= ZONE_MIN_POINTS else None


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
        if key == "objects_min_h":
            try:
                out[key] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                pass
            continue
        if key in ("mode", "objects_url", "objects_classes"):
            out[key] = str(value)
            continue
        if key in ("objects_require_motion", "objects_only_on_motion"):
            out[key] = bool(value)
            continue
        try:
            if key in (
                "width",
                "blur",
                "min_px",
                "confirm_frames",
                "confirm_window",
                "quiet_frames",
                "objects_cache_ms",
                "objects_idle_cache_ms",
                "objects_tiling",
                "objects_width",
            ):
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
    out["objects_tiling"] = max(1, min(3, int(out.get("objects_tiling") or 1)))
    out["objects_width"] = max(0, min(1920, int(out.get("objects_width") or 0)))
    try:
        out["objects_min_h"] = max(0.0, min(1.0, float(out.get("objects_min_h") or 0.0)))
    except (TypeError, ValueError):
        out["objects_min_h"] = 0.0
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
        self.sun_entity = str(merged.get(CONF_SUN_ENTITY) or "sun.sun").strip() or "sun.sun"
        try:
            self.sun_offset_min = max(-180, min(180, int(merged.get(CONF_SUN_OFFSET) or 0)))
        except (TypeError, ValueError):
            self.sun_offset_min = 0
        rules = merged.get(CONF_INTERLOCKS)
        self.interlocks = [
            normalize_interlock(r, i + 1) for i, r in enumerate(rules or []) if isinstance(r, dict)
        ][:MAX_INTERLOCKS]
        self.mute_until_raw = str(merged.get(CONF_MUTE_UNTIL) or "")
        self.mute_until = _parse_dt(self.mute_until_raw)
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
            elif key == CONF_MUTE_UNTIL:
                options[key] = str(value or "")
            elif key == CONF_INTERLOCKS and isinstance(value, list):
                options[CONF_INTERLOCKS] = [
                    normalize_interlock(r, i + 1) for i, r in enumerate(value) if isinstance(r, dict)
                ][:MAX_INTERLOCKS]
            elif key == CONF_SUN_ENTITY:
                options[key] = str(value or "sun.sun")
            elif key == CONF_SUN_OFFSET:
                try:
                    options[key] = max(-180, min(180, int(value)))
                except (TypeError, ValueError):
                    options[key] = 0
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
            "mute_until": self.mute_until_raw,
            "sun_entity": self.sun_entity,
            "sun_offset_min": self.sun_offset_min,
            "interlocks": self.interlocks,
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
