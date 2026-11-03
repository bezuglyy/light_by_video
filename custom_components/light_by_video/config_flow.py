"""Настройка «Свет по видео» через интерфейс Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CAMERA_ENTITY,
    CONF_GO2RTC_BASE,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_URL,
    CONF_SOURCE_PASS,
    CONF_SOURCE_USER,
    DEFAULT_GO2RTC_BASE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NAME,
)
from .sources import create_source

_LOGGER = logging.getLogger(__name__)


async def _check_source(hass, data: dict[str, Any]) -> None:
    """Проверить, что кадр читается (ошибка -> ValueError с текстом)."""

    class _Probe:
        camera_entity = data.get(CONF_CAMERA_ENTITY) or ""
        snapshot_url = data.get(CONF_SNAPSHOT_URL) or ""
        go2rtc_base = data.get(CONF_GO2RTC_BASE) or DEFAULT_GO2RTC_BASE
        source_user = data.get(CONF_SOURCE_USER) or ""
        source_pass = data.get(CONF_SOURCE_PASS) or ""

    source = create_source(hass, _Probe)
    try:
        if hasattr(source, "async_start"):
            await source.async_start()
        image = await source.async_get()
        if image.width < 32 or image.height < 32:
            raise ValueError("кадр слишком маленький")
    finally:
        if hasattr(source, "async_close"):
            try:
                await source.async_close()
            except Exception:  # noqa: BLE001
                pass


def _title(source: str) -> str:
    text = str(source or "").rstrip("/")
    if text.startswith("rtsp://"):
        rest = text.split("://", 1)[1]
        host = rest.split("@", 1)[1].split("/")[0] if "@" in rest else rest.split("/")[0]
        tail = rest.rstrip("/").rsplit("/", 2)
        return f"{host} · {'/'.join(tail[-2:])}"[:60]
    if "." in text and not text.startswith(("http://", "https://")):
        return text[:60]
    return text.rsplit("/", 1)[-1][:60] or NAME


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_CAMERA_ENTITY,
                description={"suggested_value": defaults.get(CONF_CAMERA_ENTITY) or None},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="camera")),
            vol.Optional(
                CONF_SNAPSHOT_URL, default=defaults.get(CONF_SNAPSHOT_URL, "")
            ): str,
            vol.Optional(
                CONF_GO2RTC_BASE,
                default=defaults.get(CONF_GO2RTC_BASE, DEFAULT_GO2RTC_BASE),
            ): str,
            vol.Optional(
                CONF_SOURCE_USER, default=defaults.get(CONF_SOURCE_USER, "")
            ): str,
            vol.Optional(
                CONF_SOURCE_PASS, default=defaults.get(CONF_SOURCE_PASS, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=30.0)),
        }
    )


class LightByVideoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Мастер добавления интеграции."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input.get(CONF_CAMERA_ENTITY) or user_input.get(
                CONF_SNAPSHOT_URL
            )
            if not source:
                errors["base"] = "no_source"
            else:
                try:
                    await _check_source(self.hass, user_input)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("light_by_video: источник не проверен: %s", err)
                    errors["base"] = "cannot_connect"
                if not errors:
                    await self.async_set_unique_id(str(source))
                    self._abort_if_unique_id_configured()
                    title = str(source).rsplit("/", 1)[-1] or NAME
                    return self.async_create_entry(
                        title=title, data=user_input, options=user_input
                    )
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "LightByVideoOptionsFlow":
        return LightByVideoOptionsFlow(config_entry)


class LightByVideoOptionsFlow(OptionsFlow):
    """Правка источника и интервала."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _check_source(self.hass, user_input)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("light_by_video: источник не проверен: %s", err)
                errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        merged = {**self._entry.data, **self._entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema(merged), errors=errors
        )
