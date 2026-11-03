"""Источники кадра: HTTP-снапшот/go2rtc, сущность camera.*, RTSP (PyAV)."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.core import HomeAssistant
from PIL import Image

from .const import DEFAULT_GO2RTC_BASE, FETCH_RETRIES, FETCH_TIMEOUT

_LOGGER = logging.getLogger(__name__)

RTSP_STALE_SEC = 15.0  # кадр старше — считаем источник недоступным
RTSP_RECONNECT_SEC = 3.0
RTSP_TIMEOUT_US = "5000000"  # 5 с: чтение из потока не блокируется навсегда


def build_http_url(value: str, go2rtc_base: str = DEFAULT_GO2RTC_BASE) -> str:
    """URL снапшота из готового адреса или имени потока go2rtc."""
    value = (value or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    base = (go2rtc_base or DEFAULT_GO2RTC_BASE).rstrip("/")
    return f"{base}/api/frame.jpeg?src={quote(value, safe='')}"


class HttpSource:
    """JPEG по HTTP (go2rtc, снапшот камеры, Line API)."""

    def __init__(self, hass: HomeAssistant, url: str, user: str = "", password: str = "") -> None:
        self.hass = hass
        self.url = url
        self._auth = aiohttp.BasicAuth(user, password) if user else None
        self._session: aiohttp.ClientSession | None = None
        self.last_bytes: bytes | None = None

    async def async_get(self) -> Image.Image:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT))
        last_error: Exception | None = None
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                async with self._session.get(self.url, auth=self._auth) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
                if len(data) < 2000:
                    raise RuntimeError(f"слишком маленький кадр: {len(data)} байт")
                self.last_bytes = data
                return await self.hass.async_add_executor_job(_open_bytes, data)
            except Exception as err:  # noqa: BLE001
                last_error = err
                if attempt < FETCH_RETRIES:
                    await asyncio.sleep(1.2 * attempt)
        raise RuntimeError(f"кадр не получен: {last_error}")

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class CameraSource:
    """Сущность camera.* Home Assistant."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.last_bytes: bytes | None = None

    async def async_get(self) -> Image.Image:
        from homeassistant.components import camera as camera_component

        image = await camera_component.async_get_image(self.hass, self.entity_id, timeout=FETCH_TIMEOUT)
        content = getattr(image, "content", None) or b""
        if len(content) < 2000:
            raise RuntimeError("камера вернула пустой кадр")
        self.last_bytes = content
        return await self.hass.async_add_executor_job(_open_bytes, content)

    async def async_close(self) -> None:
        return None


class RtspSource:
    """RTSP-поток: фоновый поток PyAV держит последний декодированный кадр.

    Нужен там, где HTTP-снапшот не работает (например, Line-камера отдаёт 500
    «no frame»), а go2rtc поток не берёт. Декодируем субпоток (обычно 640×360),
    поэтому нагрузка небольшая.
    """

    def __init__(self, hass: HomeAssistant, url: str, user: str = "", password: str = "") -> None:
        self.hass = hass
        self.url = _with_auth(url, user, password)
        self._frame: Any = None
        self._ts: float = 0.0
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------------ жизнь
    async def async_start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="light_by_video_rtsp", daemon=True)
        self._thread.start()

    async def async_close(self) -> None:
        # ВАЖНО: не закрываем контейнер из другого потока — ffmpeg/PyAV не потокобезопасен
        # (на этом HA падал с SIGSEGV). Просто просим поток завершиться и ждём.
        self._stop.set()
        thread = self._thread
        if thread is not None:
            await self.hass.async_add_executor_job(thread.join, 5.0)
        self._thread = None

    # ------------------------------------------------------------------ кадры
    def age(self) -> float:
        with self._lock:
            return (time.monotonic() - self._ts) if self._ts else 1e9

    async def async_get(self) -> Image.Image:
        if self._thread is None:
            await self.async_start()
        deadline = time.monotonic() + 7.0
        while True:
            with self._lock:
                frame = None if self._frame is None else self._frame.copy()
                ts = self._ts
            if frame is not None and (time.monotonic() - ts) <= RTSP_STALE_SEC:
                return await self.hass.async_add_executor_job(_to_image, frame)
            if time.monotonic() >= deadline:
                raise RuntimeError(self.last_error or "нет кадра с RTSP-потока")
            await asyncio.sleep(0.25)

    async def async_jpeg(self, quality: int = 75, width: int | None = None) -> bytes | None:
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            return None
        return await self.hass.async_add_executor_job(_to_jpeg, frame, quality, width)

    async def async_mask(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # ------------------------------------------------------------------ поток
    def _run(self) -> None:
        import av

        while not self._stop.is_set():
            try:
                with av.open(
                    self.url,
                    options={
                        "rtsp_transport": "tcp",
                        "rtsp_flags": "prefer_tcp",
                        "stimeout": RTSP_TIMEOUT_US,
                    },
                    timeout=10.0,
                ) as container:
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    self.last_error = ""
                    _LOGGER.info("light_by_video: RTSP-поток открыт (%s)", _hide(self.url))
                    for frame in container.decode(stream):
                        if self._stop.is_set():
                            break
                        arr = frame.to_ndarray(format="rgb24")
                        with self._lock:
                            self._frame = arr
                            self._ts = time.monotonic()
                            self._jpeg = None
            except Exception as err:  # noqa: BLE001
                self.last_error = str(err)
                _LOGGER.warning("light_by_video: RTSP ошибка (%s): %s", _hide(self.url), err)
                self._stop.wait(RTSP_RECONNECT_SEC)



def _open_bytes(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _to_image(arr) -> Image.Image:
    return Image.fromarray(arr, mode="RGB")


def _to_jpeg(arr, quality: int, width: int | None = None) -> bytes:
    image = Image.fromarray(arr, mode="RGB")
    if width and image.width > int(width):
        height = max(1, int(round(image.height * int(width) / image.width)))
        image = image.resize((int(width), height), Image.BILINEAR)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _with_auth(url: str, user: str, password: str) -> str:
    if not user or "://" not in url or "@" in url.split("://", 1)[1].split("/")[0]:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


def _hide(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host = rest.split("@", 1)[1]
    return f"{scheme}://***@{host}"


def create_source(hass: HomeAssistant, settings: Any):
    """Выбрать источник по настройкам: camera.* > RTSP > HTTP."""
    if settings.camera_entity:
        return CameraSource(hass, settings.camera_entity)
    value = (settings.snapshot_url or "").strip()
    if not value:
        raise RuntimeError("не задан источник кадра")
    if value.startswith("rtsp://"):
        return RtspSource(hass, value, settings.source_user, settings.source_pass)
    return HttpSource(
        hass,
        build_http_url(value, settings.go2rtc_base),
        settings.source_user,
        settings.source_pass,
    )
