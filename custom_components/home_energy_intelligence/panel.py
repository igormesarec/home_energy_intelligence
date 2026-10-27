"""Register the Home Energy Intelligence sidebar panel."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN, PANEL_ICON, PANEL_TITLE, PANEL_URL, VERSION


async def async_register_panel(hass) -> None:
    if frontend.async_panel_exists(hass, PANEL_URL):
        return
    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([
        StaticPathConfig(f"/{DOMAIN}_static", str(frontend_dir), True)
    ])
    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="home-energy-intelligence-panel",
        module_url=f"/{DOMAIN}_static/hei-panel.js?v={VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={},
        embed_iframe=False,
    )


def async_remove_panel(hass) -> None:
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
