from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtGui import QScreen

from logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class DisplayAssignment:
    grid_screen: QScreen
    fullscreen_screen: QScreen
    single_display_mode: bool


def _is_portrait(screen: QScreen) -> bool:
    g = screen.geometry()
    return g.height() >= g.width()


def assign_displays(screens: list[QScreen], displays_config: dict | None = None) -> DisplayAssignment:
    if not screens:
        raise RuntimeError("利用可能なディスプレイが見つかりません")

    displays_config = displays_config or {}
    grid_index = displays_config.get("grid_screen_index")
    fullscreen_index = displays_config.get("fullscreen_screen_index")

    if isinstance(grid_index, int) and 0 <= grid_index < len(screens):
        grid_screen = screens[grid_index]
    else:
        grid_screen = next((s for s in screens if _is_portrait(s)), screens[0])

    if isinstance(fullscreen_index, int) and 0 <= fullscreen_index < len(screens):
        fullscreen_screen = screens[fullscreen_index]
    else:
        fullscreen_screen = next((s for s in screens if not _is_portrait(s)), grid_screen)

    single_display_mode = len(screens) < 2 or grid_screen is fullscreen_screen
    logger.info(
        "Display assignment: grid=%s fullscreen=%s single_display_mode=%s",
        grid_screen.name(),
        fullscreen_screen.name(),
        single_display_mode,
    )
    return DisplayAssignment(grid_screen=grid_screen, fullscreen_screen=fullscreen_screen, single_display_mode=single_display_mode)
