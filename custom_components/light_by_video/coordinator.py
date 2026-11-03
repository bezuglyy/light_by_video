"""Координатор: опрос кадра, детекция движения по зонам, события."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .actions import ActionEngine
from .const import (
    DOMAIN,
    EVENT_BUS,
    EVENT_CAMERA_BACK,
    EVENT_CAMERA_LOST,
    EVENT_CLEARED,
    EVENT_DETECTED,
    EVENT_LIGHTING,
    EVENT_TITLES,
    MAX_EVENT_LOG,
    ROLE_MOTION,
)
from .motion import analyze, blur, mask_png, prepare
from .settings import Settings
from .sources import create_source

_LOGGER = logging.getLogger(__name__)

LIGHTING_EVENT_MIN_SEC = 30.0
EXTEND_MIN_SEC = 5.0


class LightByVideoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Опрашивает источник кадра и ведёт состояние зон."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, settings: Settings, actions: ActionEngine) -> None:
        self.entry = entry
        self.settings = settings
        self.actions = actions
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.title}",
            update_interval=timedelta(seconds=max(1.0, settings.scan_interval)),
        )
        self.source = None
        self.last_error: str = ""
        self.last_frame_bytes: bytes | None = None
        self.last_gray = None
        self.last_sat: float = 1.0
        self.last_metrics: dict[str, Any] = {}
        self._bg = None
        self._lock = asyncio.Lock()
        self._zstate: dict[str, dict[str, Any]] = {}
        self._last_extend: dict[str, float] = {}
        self._last_lighting = 0.0
        self._camera_ok: bool | None = None
        self._fail_streak = 0
        self._lost_emitted = False
        self._session: aiohttp.ClientSession | None = None
        self._objects: dict[str, Any] = {}
        self.event_log: deque[dict[str, Any]] = deque(maxlen=MAX_EVENT_LOG)
        self.started_at = time.time()
        self.zone_entities_applied = sorted(z["id"] for z in settings.zones if z.get("entity"))
        self.source_signature_applied = self.source_signature()
        self.last_motion_ts: str | None = None
        self.motion_count: int = 0
        self._motion_date = dt_util.now().date().isoformat()

    # ------------------------------------------------------------------ жизнь
    def source_signature(self) -> tuple:
        s = self.settings
        return (s.camera_entity, s.snapshot_url, s.source_user, s.source_pass, s.go2rtc_base)

    async def async_start(self) -> None:
        try:
            self.source = create_source(self.hass, self.settings)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("light_by_video: источник кадра не создан: %s", err)
            self.source = None
            return
        self.source_signature_applied = self.source_signature()
        if hasattr(self.source, "async_start"):
            await self.source.async_start()

    async def async_shutdown(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        if self.source is not None and hasattr(self.source, "async_close"):
            try:
                await self.source.async_close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: источник не закрыт: %s", err)
        await super().async_shutdown()

    async def async_restart_source(self) -> None:
        if self.source is not None and hasattr(self.source, "async_close"):
            try:
                await self.source.async_close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("light_by_video: источник не закрыт: %s", err)
        self._bg = None
        self._zstate.clear()
        await self.async_start()

    # ------------------------------------------------------------------ кадр
    async def async_jpeg(self) -> bytes | None:
        """Кадр для панели: сырые байты HTTP или JPEG из RTSP."""
        if self.source is not None and hasattr(self.source, "async_jpeg"):
            return await self.source.async_jpeg()
        return self.last_frame_bytes

    async def async_mask_png(self) -> bytes | None:
        if self.last_gray is None:
            return None
        bg = self._bg
        return await self.hass.async_add_executor_job(
            mask_png, self.last_gray, bg, self.settings.detector, self.settings.zones, self.last_sat
        )

    # ------------------------------------------------------------------ объекты
    async def _async_objects(self) -> dict[str, Any]:
        """Спросить ONNX-детектор объектов (не чаще objects_cache_ms)."""
        cfg = self.settings.detector
        now = time.monotonic()
        cache = self._objects or {}
        if cache and (now - float(cache.get("ts") or 0)) * 1000 < int(cfg.get("objects_cache_ms", 700)):
            return cache

        jpeg = await self.async_jpeg()
        if not jpeg:
            return {"ts": now, "objects": [], "count": 0, "ok": False, "error": "нет кадра", "ms": 0}

        url = (cfg.get("objects_url") or "").strip()
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            params = {
                "conf": cfg.get("objects_conf", 0.35),
                "classes": cfg.get("objects_classes", ""),
            }
            timeout = aiohttp.ClientTimeout(total=float(cfg.get("objects_timeout", 4.0)))
            async with self._session.post(url, data=jpeg, params=params, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
            items = data.get("objects") or []
            info = {
                "ts": now,
                "objects": items,
                "count": len(items),
                "ok": True,
                "error": "",
                "ms": data.get("infer_ms") or data.get("ms") or 0,
            }
        except Exception as err:  # noqa: BLE001
            if (self._objects or {}).get("ok") is not False:
                _LOGGER.warning(
                    "light_by_video: детектор объектов недоступен (%s) — работаю по разности кадров", err
                )
            info = {"ts": now, "objects": [], "count": 0, "ok": False, "error": str(err), "ms": 0}
        self._objects = info
        return info

    @staticmethod
    def _zone_object_hit(zone: dict[str, Any], objects: list[dict[str, Any]]) -> tuple[bool, dict[str, Any] | None]:
        """Есть ли в зоне объект (центр внутри зоны или >30 % площади объекта)."""
        x = float(zone.get("x") or 0.0)
        y = float(zone.get("y") or 0.0)
        w = float(zone.get("w") or 0.0)
        h = float(zone.get("h") or 0.0)
        best: dict[str, Any] | None = None
        for obj in objects or []:
            box = obj.get("box") or []
            if len(box) != 4:
                continue
            bx, by, bw, bh = (float(v) for v in box)
            cx, cy = bx + bw / 2.0, by + bh / 2.0
            inside = x <= cx <= x + w and y <= cy <= y + h
            ix = max(0.0, min(x + w, bx + bw) - max(x, bx))
            iy = max(0.0, min(y + h, by + bh) - max(y, by))
            area = bw * bh
            ratio = (ix * iy) / area if area > 1e-9 else 0.0
            if inside or ratio >= 0.3:
                if best is None or float(obj.get("conf") or 0) > float(best.get("conf") or 0):
                    best = obj
        return best is not None, best

    # ------------------------------------------------------------------ цикл
    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            return await self._update_once()

    def _sync_analyze(self, img) -> dict[str, Any]:
        gray, sat, size = prepare(img, int(self.settings.detector.get("width", 480)))
        radius = int(self.settings.detector.get("blur") or 0)
        if radius > 0:
            gray = blur(gray, radius)
        result = analyze(gray, self._bg, self.settings.detector, self.settings.zones, sat=sat)
        self._bg = result.pop("background", None)
        result["size"] = size
        return result, gray, sat

    async def _update_once(self) -> dict[str, Any]:
        if self.source is None:
            await self.async_start()
        if self.source is None:
            return await self._handle_failure(RuntimeError("источник кадра не настроен"))

        try:
            img = await self.source.async_get()
            result, gray, sat = await self.hass.async_add_executor_job(self._sync_analyze, img)
        except Exception as err:  # noqa: BLE001
            return await self._handle_failure(err)

        self.last_gray = gray
        self.last_sat = sat
        self.last_error = ""
        self.last_metrics = result

        self._fail_streak = 0
        self._lost_emitted = False
        if self._camera_ok is not True:
            was_lost = self._camera_ok is False
            self._camera_ok = True
            if was_lost:
                await self._emit(EVENT_CAMERA_BACK, {"reason": "кадр снова приходит"})

        if result.get("lighting"):
            now = time.monotonic()
            if now - self._last_lighting > LIGHTING_EVENT_MIN_SEC:
                self._last_lighting = now
                await self._emit(EVENT_LIGHTING, {"ratio": result.get("global_ratio")})

        mode_objects = str(self.settings.detector.get("mode") or "frame") == "objects"
        objects_info: dict[str, Any] = {"count": 0, "ok": True, "error": "", "ms": 0}
        if mode_objects:
            objects_info = await self._async_objects()

        zones_out: list[dict[str, Any]] = []
        metrics = {item.get("id"): item for item in (result.get("zones") or [])}

        if mode_objects:
            for zone in self.settings.zones:
                item = dict(
                    metrics.get(zone["id"])
                    or {
                        "id": zone["id"],
                        "name": zone.get("name"),
                        "role": zone.get("role"),
                        "ratio": 0.0,
                        "need": 0.0,
                        "changed_px": 0,
                        "px": 0,
                        "bbox": None,
                    }
                )
                if zone.get("role") != ROLE_MOTION:
                    item["active"] = False
                    zones_out.append(item)
                    continue
                if objects_info.get("ok"):
                    hit_obj, obj = self._zone_object_hit(zone, objects_info.get("objects") or [])
                    if self.settings.detector.get("objects_require_motion", True):
                        hit = bool(hit_obj) and bool(item.get("hit"))
                    else:
                        hit = bool(hit_obj)
                else:
                    hit, obj = bool(item.get("hit")), None
                item["hit"] = bool(hit)
                item["object_present"] = bool(obj)
                item["object"] = obj
                item["active"] = await self._update_zone(zone, item)
                zones_out.append(item)
        elif result.get("initialized") and not result.get("lighting"):
            for item in result.get("zones") or []:
                zone = self.settings.zone(item["id"]) or {}
                if zone.get("role") != ROLE_MOTION:
                    zones_out.append({**item, "active": False})
                    continue
                active = await self._update_zone(zone, item)
                zones_out.append({**item, "active": active})
        else:
            for zone in self.settings.zones:
                zones_out.append(
                    {
                        "id": zone["id"],
                        "name": zone.get("name"),
                        "role": zone.get("role"),
                        "ratio": 0.0,
                        "need": 0.0,
                        "changed_px": 0,
                        "px": 0,
                        "bbox": None,
                        "hit": False,
                        "active": self._zstate.get(zone["id"], {}).get("active", False),
                    }
                )

        return {
            "state": "ok" if self._camera_ok else "lost",
            "enabled": self.settings.enabled,
            "camera_ok": True,
            "ts": dt_util.utcnow().isoformat(),
            "frame": result.get("frame"),
            "ir": result.get("ir"),
            "mean": result.get("mean"),
            "global_ratio": result.get("global_ratio"),
            "lighting": result.get("lighting"),
            "reason": result.get("reason", ""),
            "zones": zones_out,
            "actions": self.actions.active_info(),
            "objects": objects_info,
            "detector_mode": str(self.settings.detector.get("mode") or "frame"),
            "last_motion": self.last_motion_ts,
            "motion_count": self.motion_count,
            "source": _source_name(self.settings),
            "last_event": self.event_log[-1] if self.event_log else None,
        }

    async def _update_zone(self, zone: dict[str, Any], item: dict[str, Any]) -> bool:
        """Временная логика зоны: подтверждение, тишина, продление."""
        zone_id = zone["id"]
        cfg = self.settings.detector
        state = self._zstate.setdefault(zone_id, {"streak": 0, "quiet": 0, "active": False})
        hit = bool(item.get("hit"))

        if hit:
            state["streak"] += 1
            state["quiet"] = 0
        else:
            state["streak"] = 0
            state["quiet"] += 1

        if not state["active"]:
            if state["streak"] >= int(cfg.get("confirm_frames", 2)):
                state["active"] = True
                today = dt_util.now().date().isoformat()
                if today != self._motion_date:
                    self._motion_date = today
                    self.motion_count = 0
                self.motion_count += 1
                self.last_motion_ts = dt_util.utcnow().isoformat()
                await self._emit(
                    EVENT_DETECTED,
                    {"zone": zone.get("name"), "zone_id": zone_id, "ratio": item.get("ratio"), "px": item.get("changed_px")},
                )
                await self.actions.async_trigger(zone, {"ratio": item.get("ratio")})
            return state["active"]

        if hit and zone.get("extend", True):
            now = time.monotonic()
            if now - self._last_extend.get(zone_id, 0) >= EXTEND_MIN_SEC:
                self._last_extend[zone_id] = now
                await self.actions.async_trigger(zone, {"ratio": item.get("ratio"), "extend": True})

        if not hit and state["quiet"] >= int(cfg.get("quiet_frames", 4)):
            state["active"] = False
            await self._emit(EVENT_CLEARED, {"zone": zone.get("name"), "zone_id": zone_id})
        return state["active"]

    async def _handle_failure(self, err: Exception) -> dict[str, Any]:
        self._fail_streak += 1
        self._camera_ok = False
        self.last_error = str(err)
        if self._fail_streak >= 3 and not self._lost_emitted:
            self._lost_emitted = True
            await self._emit(EVENT_CAMERA_LOST, {"reason": f"{err} (сбоев подряд: {self._fail_streak})"})
        _LOGGER.debug("light_by_video: кадр недоступен: %s", err)
        base = dict(self.data or {})
        if not base:
            raise UpdateFailed(str(err))
        base.update({"camera_ok": False, "state": "lost", "reason": f"источник недоступен: {err}"})
        return base

    # ------------------------------------------------------------------ события
    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        entry = {
            "ts": dt_util.utcnow().isoformat(),
            "event": event,
            "title": EVENT_TITLES.get(event, event),
            "entry_id": self.entry.entry_id,
            **payload,
        }
        self.event_log.append(entry)
        self.hass.bus.async_fire(EVENT_BUS, entry)
        _LOGGER.debug("light_by_video: событие %s %s", event, payload)


def _source_name(settings: Settings) -> str:
    if settings.camera_entity:
        return settings.camera_entity
    value = (settings.snapshot_url or "").strip()
    if value.startswith("rtsp://"):
        rest = value.split("://", 1)[1]
        return "rtsp://" + (rest.split("@", 1)[1] if "@" in rest else rest)
    return value
