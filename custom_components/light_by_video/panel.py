"""Регистрация панели «Свет по видео» в Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import PANEL_ICON, PANEL_TITLE, PANEL_URL, URL_STATIC

_LOGGER = logging.getLogger(__name__)


async def async_register_panel(hass: HomeAssistant) -> None:
    frontend_dir = Path(__file__).parent / "frontend"
    try:
        js_version = int((frontend_dir / "panel.js").stat().st_mtime)
    except OSError:
        js_version = 1
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_STATIC, str(frontend_dir), True)]
        )
    except RuntimeError:
        _LOGGER.debug("light_by_video: статика панели уже зарегистрирована")

    if PANEL_URL in hass.data.get("frontend_panels", {}):
        return
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL,
            webcomponent_name="light-by-video-panel",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            module_url=f"{URL_STATIC}/panel.js?v={js_version}",
            embed_iframe=False,
            require_admin=False,
            config={"domain": "light_by_video"},
        )
        _LOGGER.info("light_by_video: панель «%s» добавлена в меню", PANEL_TITLE)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("light_by_video: панель не добавлена: %s", err)


async def async_unregister_panel(hass: HomeAssistant) -> None:
    try:
        from homeassistant.components.frontend import async_remove_panel

        async_remove_panel(hass, PANEL_URL)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("light_by_video: панель не удалена: %s", err)
