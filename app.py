"""
AdsPower Warmup Manager v4.0.1 - Desktop Application
GPU-accelerated UI using Dear PyGui (DirectX 11 / OpenGL rendering).
"""

import dearpygui.dearpygui as dpg
import threading
import asyncio
import queue
import os
import logging
import time
import ctypes
from datetime import datetime
from typing import Dict, Optional

from core.config_manager import ConfigManager
from core.warmup_engine import WarmupEngine
from core.browser_manager import BrowserManager
from core.notifications import (
    APP_ID as _NOTIF_APP_ID,
    notify_captcha_required, notify_captcha_resolved,
    notify_profile_failed, notify_profile_completed, notify_all_done,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("warmup.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

STATUS_COLORS = {
    "waiting":     [107, 114, 128, 255],
    "starting":    [59, 130, 246, 255],
    "proxy_check": [167, 139, 250, 255],
    "proxy_ok":    [16, 185, 129, 255],
    "proxy_fail":  [239, 68, 68, 255],
    "phase1":      [245, 158, 11, 255],
    "phase2":      [249, 115, 22, 255],
    "phase3":      [139, 92, 246, 255],
    "targeted":    [236, 72, 153, 255],
    "paused":      [251, 191, 36, 255],
    "completed":   [16, 185, 129, 255],
    "failed":      [239, 68, 68, 255],
    "stopped":     [156, 163, 175, 255],
}

STATUS_LABELS = {
    "waiting": "Waiting",
    "starting": "Starting...",
    "proxy_check": "Checking Proxy...",
    "proxy_ok": "Proxy OK",
    "proxy_fail": "Proxy FAILED",
    "phase1": "Phase 1: Ramp",
    "phase2": "Phase 2: Recon",
    "phase3": "Phase 3: Idle",
    "targeted": "Step 2: Target Site",
    "paused": "|| Paused",
    "completed": "Completed",
    "failed": "Failed",
    "stopped": "Stopped",
}

_DEFAULT_API_URL = "http://local.adspower.net:50325"

LOG_COLORS = {
    "success": [74, 222, 128, 255],
    "error":   [248, 113, 113, 255],
    "warning": [251, 191, 36, 255],
    "action":  [96, 165, 250, 255],
    "info":    [156, 163, 175, 255],
    "captcha": [244, 114, 182, 255],
}


def _activity_color(text: str):
    t = text.lower()
    if any(w in t for w in ["complete", "success", "solved", "done", "finished", "ok"]):
        return [74, 222, 128, 255]
    if any(w in t for w in ["error", "failed", "blocked", "banned"]):
        return [248, 113, 113, 255]
    if any(w in t for w in ["captcha", "warning", "retry", "skip"]):
        return [251, 191, 36, 255]
    if any(w in t for w in ["google", "search", "address bar", "navigat"]):
        return [96, 165, 250, 255]
    return [156, 163, 175, 255]


def _detect_log_level(message: str, level: str) -> str:
    if level != "info":
        return level
    m = message.lower()
    if "unsuccessful" in m:
        return "error"
    if any(w in m for w in ["error", "failed after", "exception", "blocked", "banned"]):
        return "error"
    if any(w in m for w in ["completed", "success", "solved", "ok", "done", "finished", "passed"]):
        return "success"
    if any(w in m for w in ["failed", "fail"]):
        return "error"
    if any(w in m for w in ["captcha", "recaptcha", "hcaptcha"]):
        return "captcha"
    if any(w in m for w in ["warning", "retry", "timeout", "skip"]):
        return "warning"
    if any(w in m for w in ["searching", "navigating", "clicking", "typing", "scrolling",
                             "reading", "visiting", "google", "address bar"]):
        return "action"
    return "info"


def _enable_hidpi():
    """Enable per-monitor DPI awareness so DPG renders at native resolution."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _get_dpi_scale() -> float:
    """Return the primary monitor DPI scale factor (1.0 = 96 DPI / 100%)."""
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0
    except Exception:
        return 1.0


class App:
    """Main application — GPU-accelerated UI via Dear PyGui."""

    def __init__(self):
        _enable_hidpi()

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_NOTIF_APP_ID)
        except Exception:
            pass

        self._dpi_scale = _get_dpi_scale()

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config = ConfigManager(os.path.join(base_dir, "config.json"))
        self.msg_queue: queue.Queue = queue.Queue()
        self.engine: Optional[WarmupEngine] = None
        self.profile_cards: Dict[str, dict] = {}
        self.profile_info: Dict[str, dict] = self.config.get("profile_info", {})
        self.stats = {"total": 0, "running": 0, "completed": 0, "failed": 0}
        self._run_end_announced = False
        self._warmup_start_time: Optional[float] = None
        self._log_count = 0
        self._log_expanded = True
        self._log_height = 160          # current log panel height in px
        self._splitter_dragging = False
        self._splitter_drag_start_y = 0.0
        self._splitter_drag_start_log_h = 160
        self._splitter_hover = False
        self._alert_visible = False
        self._alert_flash_count = 0
        self._alert_flash_time = 0.0
        self._alert_level = "error"
        self._scheduled: list = []
        self._last_timer_tick = 0.0
        self._session_budget_s = 45 * 60

        from core.personas import get_persona_names, get_persona_hints, get_persona_labels
        self._persona_options = get_persona_names()
        self._persona_hints = get_persona_hints()
        self._persona_labels = get_persona_labels()

        dpg.create_context()
        self._setup_fonts()
        self._setup_themes()
        self._build_ui()
        self._build_dialogs()
        self._load_profiles_from_config()

        dpg.create_viewport(title="AdsPower Warmup Manager v4.0.1",
                            width=1100, height=700,
                            min_width=900, min_height=550)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        self._schedule(800, self._auto_refresh_profiles)
        self._schedule(1500, self._check_resume_available)

    # ── Scheduling helper (replaces Tkinter's after()) ────────────

    def _schedule(self, delay_ms: int, callback):
        self._scheduled.append((time.monotonic() + delay_ms / 1000, callback))

    # ── Fonts ─────────────────────────────────────────────────────

    def _sz(self, base: int) -> int:
        """Scale a pixel value by DPI factor."""
        return round(base * self._dpi_scale)

    def _setup_fonts(self):
        s = self._dpi_scale
        sz_default = round(16 * s)
        sz_large   = round(22 * s)
        sz_small   = round(13 * s)
        sz_mono    = round(14 * s)
        sz_log     = round(11 * s)   # compact log font

        windir = os.environ.get("WINDIR", "C:\\Windows")
        paths = {
            "regular": os.path.join(windir, "Fonts", "segoeui.ttf"),
            "bold":    os.path.join(windir, "Fonts", "segoeuib.ttf"),
            "mono":    os.path.join(windir, "Fonts", "consola.ttf"),
        }

        def _add_with_ranges(path, size):
            f = dpg.add_font(path, size)
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Default, parent=f)
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Cyrillic, parent=f)
            dpg.add_font_range(0x2000, 0x2BFF, parent=f)
            dpg.add_font_range(0x25A0, 0x25FF, parent=f)
            dpg.add_font_range(0x2600, 0x26FF, parent=f)
            dpg.add_font_range(0x2700, 0x27BF, parent=f)
            dpg.add_font_range(0x2190, 0x21FF, parent=f)
            return f

        with dpg.font_registry():
            if os.path.exists(paths["regular"]):
                self._font = _add_with_ranges(paths["regular"], sz_default)
                self._font_large = _add_with_ranges(paths["regular"], sz_large)
                self._font_small = _add_with_ranges(paths["regular"], sz_small)
                dpg.bind_font(self._font)
            else:
                self._font = self._font_large = self._font_small = None
            if os.path.exists(paths["bold"]):
                self._font_bold = _add_with_ranges(paths["bold"], sz_default)
                self._font_title = _add_with_ranges(paths["bold"], sz_large)
            else:
                self._font_bold = self._font_title = self._font
            if os.path.exists(paths["mono"]):
                self._font_mono = _add_with_ranges(paths["mono"], sz_mono)
                self._font_log  = _add_with_ranges(paths["mono"], sz_log)
            else:
                self._font_mono = self._font
                self._font_log  = self._font_small or self._font

    # ── Themes ────────────────────────────────────────────────────

    def _setup_themes(self):
        with dpg.theme() as self._theme_dark:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, [17, 24, 39])
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [17, 24, 39])
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, [31, 41, 55])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_Button, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_Text, [229, 231, 235])
                dpg.add_theme_color(dpg.mvThemeCol_Header, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_Separator, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, [59, 130, 246])
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, [59, 130, 246])
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, [96, 165, 250])
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, [17, 24, 39])
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, [21, 29, 43])
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, [31, 41, 55])
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, [17, 24, 39])
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, [55, 65, 81])
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, [75, 85, 99])
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, [31, 41, 55])
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, [55, 65, 81])
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 10)
        dpg.bind_theme(self._theme_dark)

        self._btn = {}
        for name, base, hover in [
            ("green",    [16, 185, 129],  [5, 150, 105]),
            ("red",      [239, 68, 68],   [220, 38, 38]),
            ("purple",   [124, 58, 237],  [109, 40, 217]),
            ("yellow",   [245, 158, 11],  [217, 119, 6]),
            ("pink",     [236, 72, 153],  [219, 39, 119]),
            ("blue",     [59, 130, 246],  [37, 99, 235]),
            ("indigo",   [99, 102, 241],  [79, 70, 229]),
            ("dark_red", [127, 29, 29],   [69, 10, 10]),
            ("teal",     [20, 184, 166],  [13, 148, 136]),
            ("slate",    [71, 85, 105],   [51, 65, 85]),
        ]:
            with dpg.theme() as t:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, base)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hover)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, hover)
            self._btn[name] = t

        with dpg.theme() as self._alert_error_bg:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [153, 27, 27])
        with dpg.theme() as self._alert_warn_bg:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [146, 64, 14])
        with dpg.theme() as self._alert_ok_bg:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [6, 95, 70])

        with dpg.theme() as self._splitter_theme_normal:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [40, 50, 65])
        with dpg.theme() as self._splitter_theme_hover:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [59, 130, 246])

        # Compact log panel — tiny item spacing so lines pack tightly
        with dpg.theme() as self._log_compact_theme:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, [13, 18, 28])
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 2, 1)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 1)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 4, 3)
            with dpg.theme_component(dpg.mvText):
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 2, 1)

    # ── Build UI ──────────────────────────────────────────────────

    def _build_ui(self):
        with dpg.window(tag="main_window"):
            with dpg.child_window(tag="alert_banner", height=self._sz(40), border=False, show=False):
                with dpg.group(horizontal=True):
                    dpg.add_text("(!)", tag="alert_icon", color=[252, 165, 165])
                    dpg.add_text("", tag="alert_text", color=[254, 226, 226])
                    dpg.add_spacer(width=20)
                    b = dpg.add_button(label="Dismiss", callback=self._dismiss_alert,
                                       width=self._sz(80))
                    dpg.bind_item_theme(b, self._btn["dark_red"])

            with dpg.group(horizontal=True):
                self._build_sidebar()
                self._build_content()

    # ── Sidebar ───────────────────────────────────────────────────

    def _build_sidebar(self):
        with dpg.child_window(width=260, border=False, tag="sidebar"):
            t1 = dpg.add_text("AdsPower Warmup")
            if self._font_title:
                dpg.bind_item_font(t1, self._font_title)
            t2 = dpg.add_text("Manager", color=[59, 130, 246])
            if self._font_title:
                dpg.bind_item_font(t2, self._font_title)
            dpg.add_separator()

            dpg.add_button(label="Test Connection", callback=self._test_connection,
                           tag="test_btn", width=-1)
            dpg.add_text("", tag="connection_label", color=[156, 163, 175])
            dpg.add_separator()

            dpg.add_text("Max Concurrent Workers", color=[156, 163, 175])
            dpg.add_input_int(tag="worker_input",
                              default_value=min(20, self.config.get("max_concurrent", 20)),
                              min_value=1, max_value=20, min_clamped=True,
                              max_clamped=True, width=-1, step=1)
            dpg.add_separator()

            t = dpg.add_text("Session time", color=[245, 158, 11])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            saved_mins = int(self.config.get("session_minutes", 45) or 45)
            saved_mins = max(15, min(120, saved_mins))
            dpg.add_text(self._session_scale_caption(saved_mins), tag="session_mins_val",
                         color=[156, 163, 175], wrap=240)
            dpg.add_slider_int(tag="session_mins_slider", default_value=saved_mins,
                               min_value=15, max_value=120, width=-1,
                               callback=self._on_session_minutes_change)
            dpg.add_text("Stops when the timer ends. Sites (15–40) and depth (3–15) scale with time.",
                         color=[107, 114, 128], wrap=240)
            dpg.add_separator()

            dpg.add_text("Default Persona", color=[156, 163, 175])
            default_persona = self._persona_options[0] if self._persona_options else "Skin Trader"
            saved_persona = self.config.get("persona_mode", default_persona)
            if saved_persona not in self._persona_options:
                saved_persona = default_persona
            dpg.add_combo(items=self._persona_options, default_value=saved_persona,
                          callback=self._on_persona_change, tag="persona_combo", width=-1)

            dpg.add_text(self._persona_labels.get(saved_persona, "Game / item focus"),
                         color=[16, 185, 129],
                         tag="persona_custom_label")
            dpg.add_input_text(tag="persona_custom_entry",
                               hint=self._persona_hints.get(saved_persona, "Custom search context..."),
                               width=-1)
            saved_custom = self.config.get("persona_custom_text", "")
            if saved_custom:
                dpg.set_value("persona_custom_entry", saved_custom)

            dpg.add_text("Optional — shapes search queries toward a game or item.",
                         color=[107, 114, 128], wrap=240)
            dpg.add_button(label="Apply to All", callback=self._apply_persona_to_all, width=-1)
            dpg.add_separator()

            b = dpg.add_button(label=">>  Start All Selected", callback=self._start_warmup,
                               tag="start_btn", width=-1, height=self._sz(38))
            dpg.bind_item_theme(b, self._btn["green"])
            if self._font_bold:
                dpg.bind_item_font(b, self._font_bold)
            b = dpg.add_button(label="[]  Stop All", callback=self._stop_warmup,
                               tag="stop_btn", width=-1, height=self._sz(38), enabled=False)
            dpg.bind_item_theme(b, self._btn["red"])
            if self._font_bold:
                dpg.bind_item_font(b, self._font_bold)
            b = dpg.add_button(label="~  Resume Previous Run", callback=self._resume_warmup,
                               tag="resume_btn", width=-1, height=self._sz(34), show=False)
            dpg.bind_item_theme(b, self._btn["indigo"])
            b = dpg.add_button(label="Check Proxies", callback=self._check_proxies,
                               tag="proxy_btn", width=-1)
            dpg.bind_item_theme(b, self._btn["teal"])
            dpg.add_separator()

            for key, label, color in [
                ("total", "Total", [229, 231, 235]),
                ("running", "Running", [59, 130, 246]),
                ("completed", "Completed", [16, 185, 129]),
                ("failed", "Failed", [239, 68, 68]),
            ]:
                with dpg.group(horizontal=True):
                    dpg.add_text(f"{label}:", color=[156, 163, 175])
                    dpg.add_text("0", tag=f"stat_{key}", color=color)
            with dpg.group(horizontal=True):
                dpg.add_text("Est. Time:", color=[156, 163, 175])
                dpg.add_text("\u2014", tag="est_time", color=[167, 139, 250])
            with dpg.group(horizontal=True):
                dpg.add_text("Elapsed:", color=[156, 163, 175])
                dpg.add_text("\u2014", tag="elapsed", color=[156, 163, 175])
            dpg.add_separator()

            t = dpg.add_text("Target Website (Step 2)", color=[245, 158, 11],
                             tag="target_section_label")
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            dpg.add_input_text(tag="target_entry",
                               hint="e.g. mannco.store or https://tf2.tm", width=-1)
            saved_target = self.config.get("target_website", "")
            if saved_target:
                dpg.set_value("target_entry", saved_target)
            dpg.add_checkbox(label="Enable Step 2 targeted warmup", tag="target_enable",
                             default_value=self.config.get("target_warmup_enabled", False))
            dpg.add_text("After general warmup, all profiles\nwill visit this site organically",
                         color=[107, 114, 128], wrap=240, tag="target_hint")
            dpg.add_separator()

            t = dpg.add_text("Bandwidth & Cost", color=[245, 158, 11])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            dpg.add_checkbox(label="YouTube browsing", tag="youtube_cb",
                             default_value=self.config.get("youtube_enabled", True))
            dpg.add_checkbox(label="Bandwidth saver (block images)", tag="bw_saver_cb",
                             default_value=self.config.get("bandwidth_saver", False))
            dpg.add_separator()

            t = dpg.add_text("Site Warmup (Direct)", color=[236, 72, 153])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            dpg.add_text("Skip general warmup \u2014 go straight\nto your site via search engines",
                         color=[107, 114, 128], wrap=240)
            dpg.add_input_text(tag="site_warmup_url", hint="e.g. mannco.store", width=-1)
            with dpg.group(horizontal=True):
                dpg.add_text("Deep links:", color=[156, 163, 175])
                dpg.add_text("12", tag="deep_links_val", color=[236, 72, 153])
            dpg.add_slider_int(tag="deep_links_slider", default_value=12,
                               min_value=5, max_value=25, width=-1,
                               callback=lambda s, a, u: dpg.set_value("deep_links_val", str(int(a))))
            with dpg.group(horizontal=True):
                dpg.add_text("Max time:", color=[156, 163, 175])
                dpg.add_text("15 min", tag="max_time_val", color=[236, 72, 153])
            dpg.add_slider_int(tag="max_time_slider", default_value=15,
                               min_value=15, max_value=120, width=-1,
                               callback=lambda s, a, u: dpg.set_value("max_time_val", f"{int(a)} min"))
            b = dpg.add_button(label="->  Start Site Warmup", callback=self._start_site_warmup,
                               tag="site_warmup_btn", width=-1, height=self._sz(34))
            dpg.bind_item_theme(b, self._btn["pink"])
            dpg.add_separator()

            b = dpg.add_button(label="Timing Settings", callback=self._open_timing_settings,
                               tag="timing_btn", width=-1)
            dpg.bind_item_theme(b, self._btn["slate"])
            dpg.add_separator()

            t = dpg.add_text("CAPTCHA Solver", color=[245, 158, 11])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            with dpg.group(horizontal=True):
                dpg.add_text("Service:", color=[156, 163, 175])
                dpg.add_combo(items=["2captcha", "anticaptcha", "capmonster"],
                              default_value=self.config.get("captcha_service", "2captcha"),
                              tag="captcha_service", width=130)
            dpg.add_text("API Key:", color=[156, 163, 175])
            dpg.add_input_text(tag="captcha_key", hint="Paste API key here...",
                               password=True, width=-1)
            saved_cap = self.config.get("captcha_api_key", "")
            if saved_cap:
                dpg.set_value("captcha_key", saved_cap)
            dpg.add_separator()
            dpg.add_checkbox(label="Windows notifications", tag="win_notify",
                             default_value=self.config.get("windows_notifications_enabled", True))
            dpg.add_text("Toast pop-ups for CAPTCHA & events", color=[107, 114, 128])

        self._update_persona_fields(saved_persona)

    # ── Content area ──────────────────────────────────────────────

    def _build_content(self):
        with dpg.child_window(tag="content", border=False, width=-1):
            with dpg.group(horizontal=True):
                t = dpg.add_text("Profiles")
                if self._font_bold:
                    dpg.bind_item_font(t, self._font_bold)
                dpg.add_spacer(width=20)
                dpg.add_text("0 profiles", tag="profile_count", color=[156, 163, 175])
                dpg.add_spacer(width=10)
                dpg.add_text("", tag="selected_count", color=[156, 163, 175])

            with dpg.group(horizontal=True):
                b = dpg.add_button(label="X Remove Selected",
                                   callback=self._remove_selected, tag="remove_btn")
                dpg.bind_item_theme(b, self._btn["red"])
                dpg.add_spacer(width=10)
                b = dpg.add_button(label="Fetch Profiles from AdsPower",
                                   callback=self._fetch_profiles, tag="fetch_btn")
                dpg.bind_item_theme(b, self._btn["blue"])

            with dpg.group(horizontal=True):
                dpg.add_checkbox(label="Select / Deselect All", tag="select_all_cb",
                                 default_value=True, callback=self._toggle_select_all)

            dpg.add_text("No profiles loaded.\nClick 'Fetch Profiles from AdsPower' above.",
                         tag="no_profiles_label", color=[107, 114, 128])

            with dpg.child_window(tag="profile_area", height=-210, border=True):
                with dpg.table(tag="profile_table", header_row=True,
                               borders_innerH=True, borders_outerH=True,
                               borders_innerV=False, borders_outerV=True,
                               scrollY=True, scrollX=False,
                               resizable=True, sortable=False,
                               policy=dpg.mvTable_SizingStretchProp,
                               row_background=True):
                    dpg.add_table_column(label="", width=28, width_fixed=True, no_resize=True)
                    dpg.add_table_column(label="Name", init_width_or_weight=1.2)
                    dpg.add_table_column(label="Proxy", init_width_or_weight=1.4)
                    dpg.add_table_column(label="Remark", init_width_or_weight=1.0)
                    dpg.add_table_column(label="Persona", init_width_or_weight=1.0)
                    dpg.add_table_column(label="Status", init_width_or_weight=1.0)
                    dpg.add_table_column(label="Target", init_width_or_weight=1.0)
                    dpg.add_table_column(label="Score", width=50, width_fixed=True, no_resize=True)
                    dpg.add_table_column(label="Actions", init_width_or_weight=1.2)

            # ── Drag handle ───────────────────────────────────────
            with dpg.child_window(tag="splitter_handle", height=10, border=False):
                dpg.add_text("~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~",
                             tag="splitter_dots", color=[75, 85, 99])
            dpg.bind_item_theme("splitter_handle", self._splitter_theme_normal)

            # Register mouse handlers on the handle
            with dpg.item_handler_registry(tag="splitter_reg"):
                dpg.add_item_hover_handler(callback=self._splitter_hovered)
            dpg.bind_item_handler_registry("splitter_handle", "splitter_reg")

            with dpg.group(horizontal=True, tag="log_header"):
                t = dpg.add_text("Activity Log")
                if self._font_bold:
                    dpg.bind_item_font(t, self._font_bold)
                dpg.add_spacer(width=10)
                dpg.add_button(label="Hide / Show", callback=self._log_toggle_visibility,
                               tag="log_toggle_btn", width=90)
                dpg.add_button(label="Clear", callback=self._clear_log, width=60)

            with dpg.child_window(tag="log_content", height=160, border=True):
                pass
            dpg.bind_item_theme("log_content", self._log_compact_theme)

    # ── Dialogs ───────────────────────────────────────────────────

    def _build_dialogs(self):
        with dpg.window(label="Profile Settings", modal=True, show=False,
                        tag="profile_dlg", width=440, height=480, no_resize=True,
                        on_close=lambda: dpg.configure_item("profile_dlg", show=False)):
            dpg.add_text("", tag="pdlg_header")
            dpg.add_text("", tag="pdlg_notice", color=[245, 158, 11], show=False)
            dpg.add_separator()
            dpg.add_text("Sites to visit (optional override):")
            dpg.add_input_int(tag="pdlg_sites", default_value=0,
                              min_value=0, max_value=40, min_clamped=True,
                              max_clamped=True, width=-1, step=1)
            dpg.add_text("0 = auto from session time (15–40). Custom clamp 15–40.",
                         color=[107, 114, 128])
            dpg.add_separator()
            t = dpg.add_text("Target Website (Step 2)", color=[245, 158, 11])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            dpg.add_input_text(tag="pdlg_target",
                               hint="e.g. mannco.store or https://tf2.tm", width=-1)
            dpg.add_checkbox(label="Enable Step 2 targeted warmup", tag="pdlg_target_enable")
            dpg.add_text("After warmup, profile visits target organically",
                         color=[107, 114, 128])
            dpg.add_separator()
            t = dpg.add_text("Bandwidth & Cost", color=[245, 158, 11])
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            dpg.add_checkbox(label="YouTube browsing", tag="pdlg_youtube",
                             default_value=True)
            dpg.add_checkbox(label="Bandwidth saver (block images)", tag="pdlg_bw_saver")
            dpg.add_separator()
            dpg.add_checkbox(label="Use custom settings (override global)",
                             tag="pdlg_use_custom", default_value=False)
            dpg.add_text("When unchecked, uses global sidebar settings",
                         color=[107, 114, 128])
            dpg.add_separator()
            with dpg.group(horizontal=True):
                b = dpg.add_button(label="Save", callback=self._save_profile_settings, width=100)
                dpg.bind_item_theme(b, self._btn["green"])
                b = dpg.add_button(label="Cancel", width=100,
                                   callback=lambda: dpg.configure_item("profile_dlg", show=False))

        with dpg.window(label="Timing Settings", modal=True, show=False,
                        tag="timing_dlg", width=460, height=560, no_resize=True,
                        on_close=lambda: dpg.configure_item("timing_dlg", show=False)):
            dpg.add_text("Timing Configuration")
            dpg.add_separator()
            self._timing_entries = {}
            for key, label, default in [
                ("action_delay_min", "Action Delay Min (s)", "8"),
                ("action_delay_max", "Action Delay Max (s)", "25"),
                ("scroll_min_px", "Scroll Min (px)", "200"),
                ("scroll_max_px", "Scroll Max (px)", "600"),
                ("idle_min_minutes", "Idle Phase Min (min)", "30"),
                ("idle_max_minutes", "Idle Phase Max (min)", "60"),
                ("tabs_min", "Tabs Min", "3"),
                ("tabs_max", "Tabs Max", "5"),
                ("typing_delay_min_ms", "Typing Delay Min (ms)", "50"),
                ("typing_delay_max_ms", "Typing Delay Max (ms)", "200"),
                ("page_load_timeout_ms", "Page Load Timeout (ms)", "30000"),
            ]:
                with dpg.group(horizontal=True):
                    dpg.add_text(label)
                    dpg.add_input_text(tag=f"tdlg_{key}", width=80, decimal=True)
                self._timing_entries[key] = f"tdlg_{key}"
            dpg.add_separator()
            t = dpg.add_text("Launch Settings")
            if self._font_bold:
                dpg.bind_item_font(t, self._font_bold)
            for key, label, default in [
                ("launch_delay_min", "Launch Stagger Min (s)", "10"),
                ("launch_delay_max", "Launch Stagger Max (s)", "30"),
            ]:
                with dpg.group(horizontal=True):
                    dpg.add_text(label)
                    dpg.add_input_text(tag=f"tdlg_{key}", width=80, decimal=True)
                self._timing_entries[key] = f"tdlg_{key}"
            dpg.add_separator()
            with dpg.group(horizontal=True):
                b = dpg.add_button(label="Save", callback=self._save_timing_settings, width=100)
                dpg.bind_item_theme(b, self._btn["green"])
                b = dpg.add_button(label="Cancel", width=100,
                                   callback=lambda: dpg.configure_item("timing_dlg", show=False))

    # ── Profile management ────────────────────────────────────────

    def _load_profiles_from_config(self):
        profiles = self.config.get("profiles", [])
        if not profiles:
            self._rebuild_profile_table([])
            return
        has_names = any(self.profile_info.get(pid, {}).get("name") for pid in profiles)
        self._rebuild_profile_table(profiles)
        if not has_names:
            self._add_log("APP", "Profile details missing \u2014 auto-fetching from AdsPower...")
            self._schedule(1500, self._fetch_profiles)

    def _rebuild_profile_table(self, profile_ids: list):
        for pid in list(self.profile_cards.keys()):
            if dpg.does_item_exist(f"row_{pid}"):
                dpg.delete_item(f"row_{pid}")
        self.profile_cards.clear()

        if not profile_ids:
            dpg.configure_item("no_profiles_label", show=True)
            dpg.set_value("profile_count", "0 profiles")
            self._update_stats(total=0)
            return

        dpg.configure_item("no_profiles_label", show=False)
        dpg.set_value("profile_count", f"{len(profile_ids)} profiles")

        fallback_persona = self._persona_options[0] if self._persona_options else "Skin Trader"
        default_persona = dpg.get_value("persona_combo") if dpg.does_item_exist("persona_combo") else fallback_persona
        p_opts = self._persona_options

        for i, pid in enumerate(profile_ids):
            info = self.profile_info.get(pid, {})
            saved_p = info.get("persona", default_persona)
            if saved_p not in p_opts:
                saved_p = fallback_persona
            self._add_profile_row(pid, info, saved_p, p_opts)

        self._update_stats(total=len(profile_ids))
        self._update_selected_count()

    def _add_profile_row(self, pid: str, info: dict, persona: str, p_opts: list):
        name = info.get("name", "") or pid
        country = info.get("country", "")
        remark = info.get("remark", "")
        c_upper = country.upper() if country else ""
        target_url = info.get("target_website", self.config.get("target_website", ""))
        target_enabled = info.get("target_warmup_enabled",
                                  self.config.get("target_warmup_enabled", False))
        remark_display = remark.replace("\n", " ").strip()[:35] if remark else ""

        with dpg.table_row(parent="profile_table", tag=f"row_{pid}"):
            # Sel
            dpg.add_checkbox(tag=f"{pid}_cb", default_value=True,
                             callback=lambda s, a, u: self._update_selected_count())
            # Name
            dpg.add_text(name[:20], tag=f"{pid}_name")
            # Proxy: country code + check button + short result
            with dpg.group(horizontal=True):
                if c_upper:
                    dpg.add_text(c_upper, color=[107, 180, 255])
                b = dpg.add_button(label="Test", tag=f"{pid}_proxy_btn",
                                   callback=self._on_proxy_check_single, user_data=pid)
                dpg.bind_item_theme(b, self._btn["teal"])
                dpg.add_text("", tag=f"{pid}_proxy_ip", color=[107, 114, 128])
            # Remark
            dpg.add_text(remark_display, tag=f"{pid}_remark", color=[107, 114, 128])
            # Persona
            dpg.add_combo(items=p_opts, default_value=persona, tag=f"{pid}_persona",
                          width=-1, callback=self._on_row_persona_change, user_data=pid)
            # Status (single line, short)
            dpg.add_text("Waiting", tag=f"{pid}_status", color=[156, 163, 175])
            # Target
            with dpg.group(horizontal=True):
                dpg.add_checkbox(tag=f"{pid}_target_cb", default_value=target_enabled)
                t_display = target_url[:16] if target_url else "--"
                dpg.add_text(t_display, tag=f"{pid}_target_text", color=[156, 163, 175])
            # Score
            dpg.add_text("", tag=f"{pid}_score", color=[107, 114, 128])
            # Actions: Settings / Start-Stop / Skip / Pause
            with dpg.group(horizontal=True):
                dpg.add_button(label="Cfg", tag=f"{pid}_settings_btn",
                               callback=self._on_open_profile_settings, user_data=pid)
                b = dpg.add_button(label="Start", tag=f"{pid}_primary_btn",
                                   callback=self._on_primary_action, user_data=pid)
                dpg.bind_item_theme(b, self._btn["green"])
                b = dpg.add_button(label="Skip", tag=f"{pid}_secondary_btn",
                                   callback=self._on_secondary_action, user_data=pid,
                                   show=False)
                dpg.bind_item_theme(b, self._btn["yellow"])
                b = dpg.add_button(label="Pause", tag=f"{pid}_tertiary_btn",
                                   callback=self._on_tertiary_action, user_data=pid,
                                   show=False)
                dpg.bind_item_theme(b, self._btn["blue"])

        self.profile_cards[pid] = {"status": "waiting", "score": None, "error_count": 0,
                                   "country": c_upper, "activity": "", "run_start": None,
                                   "session_seconds": None}

    def _update_row_buttons(self, pid: str, status: str):
        active = {"phase1", "phase2", "phase3", "targeted", "starting", "proxy_check"}
        finished = {"completed", "failed", "stopped", "proxy_ok", "proxy_fail"}
        if not dpg.does_item_exist(f"{pid}_primary_btn"):
            return
        if status == "paused":
            dpg.configure_item(f"{pid}_primary_btn", label="Stop")
            dpg.bind_item_theme(f"{pid}_primary_btn", self._btn["red"])
            dpg.configure_item(f"{pid}_secondary_btn", label="Resume", show=True)
            dpg.bind_item_theme(f"{pid}_secondary_btn", self._btn["green"])
            dpg.configure_item(f"{pid}_tertiary_btn", show=False)
        elif status in active:
            dpg.configure_item(f"{pid}_primary_btn", label="Stop")
            dpg.bind_item_theme(f"{pid}_primary_btn", self._btn["red"])
            dpg.configure_item(f"{pid}_secondary_btn", label="Skip", show=True)
            dpg.bind_item_theme(f"{pid}_secondary_btn", self._btn["yellow"])
            dpg.configure_item(f"{pid}_tertiary_btn", label="Pause", show=True)
        elif status in finished:
            dpg.configure_item(f"{pid}_primary_btn", label="Start")
            dpg.bind_item_theme(f"{pid}_primary_btn", self._btn["green"])
            dpg.configure_item(f"{pid}_secondary_btn", show=False)
            dpg.configure_item(f"{pid}_tertiary_btn", show=False)
        else:
            dpg.configure_item(f"{pid}_primary_btn", label="Start")
            dpg.bind_item_theme(f"{pid}_primary_btn", self._btn["green"])
            dpg.configure_item(f"{pid}_secondary_btn", show=False)
            dpg.configure_item(f"{pid}_tertiary_btn", show=False)

    def _update_profile_status(self, pid: str, status: str):
        card = self.profile_cards.get(pid)
        if not card:
            return
        if status.startswith("score:"):
            try:
                score = int(status.split(":")[1])
                card["score"] = score
                self._show_score(pid, score)
            except (ValueError, IndexError):
                pass
            return
        card["status"] = status
        color = STATUS_COLORS.get(status, [107, 114, 128, 255])
        label = STATUS_LABELS.get(status, status)
        active = {"phase1", "phase2", "phase3", "targeted", "starting", "paused"}
        if status in active and card.get("run_start") is None:
            card["run_start"] = time.monotonic()
        if status in {"completed", "failed", "stopped"}:
            card["run_start"] = None
        if dpg.does_item_exist(f"{pid}_status"):
            dpg.configure_item(f"{pid}_status", default_value=label, color=color)
        self._update_row_buttons(pid, status)

    def _show_score(self, pid: str, score: int):
        if score >= 85:
            color = [16, 185, 129, 255]
        elif score >= 70:
            color = [59, 130, 246, 255]
        elif score >= 50:
            color = [245, 158, 11, 255]
        elif score >= 30:
            color = [249, 115, 22, 255]
        else:
            color = [239, 68, 68, 255]
        if dpg.does_item_exist(f"{pid}_score"):
            dpg.configure_item(f"{pid}_score", default_value=f"{score}/100", color=color)

    def _update_activity(self, pid: str, text: str):
        card = self.profile_cards.get(pid)
        if card is not None:
            card["activity"] = text
            if card.get("run_start") is None and card.get("status") not in (
                    "waiting", "completed", "failed", "stopped"):
                card["run_start"] = time.monotonic()
        self._refresh_activity_display(pid, text)

    def _refresh_activity_display(self, pid: str, text: str):
        if not dpg.does_item_exist(f"{pid}_status"):
            return
        cd = self._card_countdown(pid)
        raw = text or ""
        if cd:
            raw = f"{cd} | {raw}"
        display = raw[:38] + "..." if len(raw) > 38 else raw
        dpg.configure_item(f"{pid}_status", default_value=display,
                           color=_activity_color(text or ""))

    def _update_error(self, pid: str, text: str):
        card = self.profile_cards.get(pid)
        if not card:
            return
        card["error_count"] = card.get("error_count", 0) + 1
        if dpg.does_item_exist(f"{pid}_status"):
            dpg.configure_item(f"{pid}_status",
                               default_value="Error",
                               color=[248, 113, 113, 255])

    def _update_proxy_result(self, pid: str, ok: bool, ip: str = "", error: str = ""):
        if dpg.does_item_exist(f"{pid}_proxy_btn"):
            dpg.configure_item(f"{pid}_proxy_btn", enabled=True)
        if ok:
            if dpg.does_item_exist(f"{pid}_proxy_ip"):
                dpg.configure_item(f"{pid}_proxy_ip",
                                   default_value=ip[:18] if ip else "OK",
                                   color=[16, 185, 129, 255])
            if dpg.does_item_exist(f"{pid}_proxy_btn"):
                dpg.configure_item(f"{pid}_proxy_btn", label="OK")
                dpg.bind_item_theme(f"{pid}_proxy_btn", self._btn["green"])
        else:
            if dpg.does_item_exist(f"{pid}_proxy_ip"):
                dpg.configure_item(f"{pid}_proxy_ip",
                                   default_value="FAIL",
                                   color=[239, 68, 68, 255])
            if dpg.does_item_exist(f"{pid}_proxy_btn"):
                dpg.configure_item(f"{pid}_proxy_btn", label="FAIL")
                dpg.bind_item_theme(f"{pid}_proxy_btn", self._btn["red"])

    # ── Per-row action button callbacks ───────────────────────────

    def _on_primary_action(self, sender, app_data, user_data):
        pid = user_data
        status = self.profile_cards.get(pid, {}).get("status", "waiting")
        active = {"phase1", "phase2", "phase3", "targeted", "starting", "proxy_check", "paused"}
        if status in active:
            self._on_stop_single_profile(pid)
        else:
            self._on_start_single_profile(pid)

    def _on_secondary_action(self, sender, app_data, user_data):
        pid = user_data
        status = self.profile_cards.get(pid, {}).get("status", "waiting")
        if status == "paused":
            self._on_resume_profile(pid)
        else:
            self._on_skip_profile(pid)

    def _on_tertiary_action(self, sender, app_data, user_data):
        pid = user_data
        self._on_pause_profile(pid)

    def _on_row_persona_change(self, sender, app_data, user_data):
        pid = user_data
        value = app_data
        self.profile_info.setdefault(pid, {})["persona"] = value

    # ── Helper: selection ─────────────────────────────────────────

    def _toggle_select_all(self, sender=None, app_data=None, user_data=None):
        state = dpg.get_value("select_all_cb")
        for pid in self.profile_cards:
            if dpg.does_item_exist(f"{pid}_cb"):
                dpg.set_value(f"{pid}_cb", state)
        self._update_selected_count()

    def _get_selected_profiles(self) -> list:
        return [pid for pid in self.profile_cards
                if dpg.does_item_exist(f"{pid}_cb") and dpg.get_value(f"{pid}_cb")]

    def _update_selected_count(self):
        selected = len(self._get_selected_profiles())
        total = len(self.profile_cards)
        if total == 0:
            dpg.set_value("selected_count", "")
        else:
            dpg.set_value("selected_count", f"{selected} of {total} selected")

    def _get_profile_personas(self) -> dict:
        return {pid: dpg.get_value(f"{pid}_persona")
                for pid in self.profile_cards
                if dpg.does_item_exist(f"{pid}_persona")}

    # ── Engine callbacks (called from engine thread) ──────────────

    def _on_engine_status(self, profile_id: str, status: str):
        self.msg_queue.put({"type": "status", "profile_id": profile_id, "status": status})

    def _on_engine_log(self, profile_id: str, message: str):
        self.msg_queue.put({"type": "log", "profile_id": profile_id, "message": message})

    def _on_engine_activity(self, profile_id: str, text: str):
        self.msg_queue.put({"type": "activity", "profile_id": profile_id, "text": text})

    def _on_engine_error(self, profile_id: str, text: str):
        self.msg_queue.put({"type": "error", "profile_id": profile_id, "text": text})

    def _on_engine_notify(self, profile_id: str, event_type: str, detail: str):
        self.msg_queue.put({"type": "notify", "profile_id": profile_id,
                            "event_type": event_type, "detail": detail})

    def _on_skip_profile(self, pid: str):
        if self.engine and self.engine.is_running():
            self.engine.skip_profile(pid)
            self._add_log(pid, "Skip requested by user")

    def _on_pause_profile(self, pid: str):
        if self.engine and self.engine.is_running():
            self.engine.pause_profile(pid)
            self._add_log(pid, "Paused \u2014 browser stays open for manual work")

    def _on_resume_profile(self, pid: str):
        if self.engine and self.engine.is_running():
            self.engine.resume_profile(pid)
            self._add_log(pid, "Resumed \u2014 warmup continuing")

    def _profile_is_active_in_engine(self, pid: str) -> bool:
        if not self.engine:
            return False
        m = self.engine._metrics.get(pid)
        if m and not m.finished_at:
            return True
        late = getattr(self.engine, "_late_profiles", None)
        return bool(late and str(pid) in late)

    def _add_profile_to_running_engine(self, pid: str) -> bool:
        """Join an idle profile to the current run. True if it was queued."""
        if not self.engine or not self.engine.is_running():
            return False
        if self._profile_is_active_in_engine(pid):
            self._add_log(pid, "Already running")
            return False
        self._stamp_session_budget([pid])
        card = self.profile_cards.get(pid)
        if card:
            card["status"] = "waiting"
            card["error_count"] = 0
            self._update_profile_status(pid, "waiting")
            self._update_activity(pid, "")
        info = self.profile_info.get(pid, {})
        if info.get("use_custom_settings", False):
            overrides = self.engine.config.get("profile_overrides", {})
            override = {}
            for key in ("sites_per_profile", "target_website", "target_warmup_enabled",
                        "youtube_enabled", "bandwidth_saver"):
                if key in info:
                    override[key] = info[key]
            if override:
                overrides[pid] = override
                self.engine.config["profile_overrides"] = overrides
        personas = self.engine.config.setdefault("profile_personas", {})
        if dpg.does_item_exist(f"{pid}_persona"):
            personas[pid] = dpg.get_value(f"{pid}_persona")
        self._add_log(pid, "Adding to running engine...")
        self.stats["total"] = self.stats.get("total", 0) + 1
        self._run_end_announced = False
        self._refresh_stats()
        self.engine.start_single_profile(pid, profile_info=info)
        return True

    def _on_start_single_profile(self, pid: str):
        if self.engine and self.engine.is_running():
            self._add_profile_to_running_engine(pid)
            return

        self._save_settings_to_config()
        self._stamp_session_budget([pid])
        card = self.profile_cards.get(pid)
        if card:
            card["status"] = "waiting"
            card["error_count"] = 0
            self._update_profile_status(pid, "waiting")
            self._update_activity(pid, "")

        self.stats["total"] = max(self.stats.get("total", 0), 1)
        self._refresh_stats()
        config_data = self._build_engine_config([pid])
        try:
            self.engine = WarmupEngine(
                config=config_data,
                on_status=self._on_engine_status, on_log=self._on_engine_log,
                on_activity=self._on_engine_activity, on_error=self._on_engine_error,
                on_notify=self._on_engine_notify,
            )
            self.engine.start([pid])
        except Exception as e:
            self._add_log("APP", f"Failed to create engine: {e}")
            return
        dpg.configure_item("start_btn", enabled=True)
        dpg.configure_item("site_warmup_btn", enabled=False)
        dpg.configure_item("stop_btn", enabled=True)
        dpg.configure_item("resume_btn", show=False)
        if self._warmup_start_time is None:
            self._start_elapsed_timer()
        self._add_log("APP", f"Single-profile warmup started: {pid}")

    def _on_stop_single_profile(self, pid: str):
        if self.engine and self.engine.is_running():
            self.engine.stop_single_profile(pid)
            self._add_log(pid, "Stop requested \u2014 aborting warmup")
            self._update_profile_status(pid, "stopped")

    def _on_open_profile_settings(self, sender, app_data, user_data):
        pid = user_data
        info = self.profile_info.get(pid, {})
        display_name = info.get("name", pid)
        dpg.set_value("pdlg_header", f"Settings \u2014 {display_name}")
        dpg.configure_item("pdlg_notice", show=False)

        raw_sites = info.get("sites_per_profile") if info.get("use_custom_settings") else None
        try:
            raw_sites = int(raw_sites) if raw_sites is not None else 0
        except (TypeError, ValueError):
            raw_sites = 0
        dpg.set_value("pdlg_sites", raw_sites if raw_sites > 0 else 0)
        dpg.set_value("pdlg_target", info.get("target_website",
                                               self.config.get("target_website", "")))
        dpg.set_value("pdlg_target_enable", info.get("target_warmup_enabled",
                                                      self.config.get("target_warmup_enabled", False)))
        dpg.set_value("pdlg_youtube", info.get("youtube_enabled",
                                                self.config.get("youtube_enabled", True)))
        dpg.set_value("pdlg_bw_saver", info.get("bandwidth_saver",
                                                  self.config.get("bandwidth_saver", False)))
        dpg.set_value("pdlg_use_custom", info.get("use_custom_settings", False))

        self._editing_profile = pid
        dpg.configure_item("profile_dlg", show=True)

    def _save_profile_settings(self):
        pid = self._editing_profile
        if pid not in self.profile_info:
            self.profile_info[pid] = {}
        sites_val = int(dpg.get_value("pdlg_sites") or 0)
        settings = {
            "use_custom_settings": dpg.get_value("pdlg_use_custom"),
            "target_website": dpg.get_value("pdlg_target").strip(),
            "target_warmup_enabled": dpg.get_value("pdlg_target_enable"),
            "youtube_enabled": dpg.get_value("pdlg_youtube"),
            "bandwidth_saver": dpg.get_value("pdlg_bw_saver"),
        }
        if sites_val >= 15:
            settings["sites_per_profile"] = min(40, sites_val)
        else:
            self.profile_info[pid].pop("sites_per_profile", None)
        self.profile_info[pid].update(settings)

        if dpg.does_item_exist(f"{pid}_target_cb"):
            dpg.set_value(f"{pid}_target_cb", settings["target_warmup_enabled"])
        if dpg.does_item_exist(f"{pid}_target_text"):
            t = settings["target_website"] or "\u2014"
            dpg.configure_item(f"{pid}_target_text", default_value=t[:18])

        self.config.set("profile_info", self.profile_info)
        self.config.save()
        dpg.configure_item("profile_dlg", show=False)

        custom = settings["use_custom_settings"]
        if custom:
            site_note = f"{settings.get('sites_per_profile', 'auto')} sites"
            self._add_log(pid, f"Custom settings saved: {site_note}, "
                               f"target={'ON' if settings['target_warmup_enabled'] else 'OFF'}")
        else:
            self._add_log(pid, "Settings saved (using global defaults)")

    def _open_timing_settings(self):
        timing = self.config.data.get("timing", {})
        timing_defaults = {
            "action_delay_min": "8", "action_delay_max": "25",
            "scroll_min_px": "200", "scroll_max_px": "600",
            "idle_min_minutes": "30", "idle_max_minutes": "60",
            "tabs_min": "3", "tabs_max": "5",
            "typing_delay_min_ms": "50", "typing_delay_max_ms": "200",
            "page_load_timeout_ms": "30000",
        }
        launch_defaults = {"launch_delay_min": "10", "launch_delay_max": "30"}

        for key, tag in self._timing_entries.items():
            if key in ("launch_delay_min", "launch_delay_max"):
                val = str(self.config.data.get(key, launch_defaults.get(key, "")))
            else:
                val = str(timing.get(key, timing_defaults.get(key, "")))
            dpg.set_value(tag, val)

        dpg.configure_item("timing_dlg", show=True)

    def _save_timing_settings(self):
        timing_keys = ["action_delay_min", "action_delay_max", "scroll_min_px", "scroll_max_px",
                        "idle_min_minutes", "idle_max_minutes", "tabs_min", "tabs_max",
                        "typing_delay_min_ms", "typing_delay_max_ms", "page_load_timeout_ms"]
        top_keys = ["launch_delay_min", "launch_delay_max"]

        _INT_KEYS = {"scroll_min_px", "scroll_max_px", "tabs_min", "tabs_max"}
        for key in timing_keys:
            try:
                raw = float(dpg.get_value(self._timing_entries[key]))
                value = int(round(raw)) if key in _INT_KEYS else raw
                self.config.set_timing(key, value)
            except (ValueError, TypeError):
                pass
        for key in top_keys:
            try:
                value = float(dpg.get_value(self._timing_entries[key]))
                self.config.set(key, value)
            except (ValueError, TypeError):
                pass
        self.config.save()
        dpg.configure_item("timing_dlg", show=False)
        self._add_log("APP", "Timing settings saved")

    # ── Action handlers ───────────────────────────────────────────

    def _build_engine_config(self, profile_ids: list) -> dict:
        for pid in profile_ids:
            if dpg.does_item_exist(f"{pid}_target_cb"):
                self.profile_info.setdefault(pid, {})["target_warmup_enabled"] = \
                    dpg.get_value(f"{pid}_target_cb")
        config_data = dict(self.config.data)
        profile_overrides = {}
        for pid in profile_ids:
            info = self.profile_info.get(pid, {})
            if info.get("use_custom_settings", False):
                override = {}
                for key in ("sites_per_profile", "target_website", "target_warmup_enabled",
                            "youtube_enabled", "bandwidth_saver"):
                    if key in info:
                        override[key] = info[key]
                if override:
                    profile_overrides[pid] = override
        config_data["profile_overrides"] = profile_overrides
        info_map = {}
        for pid in profile_ids:
            key = str(pid)
            info_map[key] = self.profile_info.get(pid) or self.profile_info.get(key) or {}
        config_data["profile_info"] = info_map
        return config_data

    def _start_warmup(self, sender=None, app_data=None, user_data=None):
        profiles = self._get_selected_profiles()
        if not profiles:
            self._add_log("APP", "No profiles selected.")
            return
        self._save_settings_to_config()

        if self.engine and self.engine.is_running():
            added = 0
            skipped = 0
            for pid in profiles:
                if self._add_profile_to_running_engine(pid):
                    added += 1
                else:
                    skipped += 1
            if added:
                self._add_log(
                    "APP",
                    f"Added {added} profile(s) to the running warmup"
                    + (f" ({skipped} already running)" if skipped else ""),
                )
            elif skipped:
                self._add_log("APP", "Selected profiles are already running")
            return

        self._stamp_session_budget(profiles)

        for pid in profiles:
            card = self.profile_cards.get(pid)
            if card:
                card["status"] = "waiting"
                card["error_count"] = 0
                self._update_profile_status(pid, "waiting")
                self._update_activity(pid, "")
        self.stats = {"total": len(profiles), "running": 0, "completed": 0, "failed": 0}
        self._run_end_announced = False
        self._refresh_stats()

        config_data = self._build_engine_config(profiles)
        try:
            self.engine = WarmupEngine(
                config=config_data,
                on_status=self._on_engine_status, on_log=self._on_engine_log,
                on_activity=self._on_engine_activity, on_error=self._on_engine_error,
                on_notify=self._on_engine_notify,
            )
            self.engine.start(profiles)
        except Exception as e:
            self._add_log("APP", f"Failed to create engine: {e}")
            dpg.configure_item("start_btn", enabled=True)
            return

        dpg.configure_item("start_btn", enabled=True)
        dpg.configure_item("site_warmup_btn", enabled=False)
        dpg.configure_item("stop_btn", enabled=True)
        dpg.configure_item("resume_btn", show=False)
        self._start_elapsed_timer()
        mins = int(self.config.get("session_minutes", 45) or 45)
        workers = dpg.get_value("worker_input")
        self._add_log("APP", f"Warmup started ({mins}m session) for {len(profiles)} profiles "
                             f"(max {workers} concurrent)")

    def _stop_warmup(self, sender=None, app_data=None, user_data=None):
        if self.engine:
            self.engine.stop()
            self._add_log("APP", "Stop signal sent. Waiting for profiles to finish...")
            dpg.configure_item("stop_btn", enabled=False)

            def _force_reenable():
                dpg.configure_item("start_btn", enabled=True)
                dpg.configure_item("site_warmup_btn", enabled=True)
                dpg.configure_item("stop_btn", enabled=False)
                dpg.configure_item("proxy_btn", enabled=True, label="Check Proxies")
                if self.engine and not self.engine.is_running():
                    self._warmup_start_time = None
                    self.engine = None
            self._schedule(8000, _force_reenable)

    def _start_site_warmup(self, sender=None, app_data=None, user_data=None):
        profiles = self._get_selected_profiles()
        if not profiles:
            self._add_log("APP", "No profiles selected.")
            return
        target_url = dpg.get_value("site_warmup_url").strip()
        if not target_url:
            self._add_log("APP", "Enter a target website URL for site warmup.")
            return
        deep_links = int(dpg.get_value("deep_links_slider"))
        max_minutes = int(dpg.get_value("max_time_slider"))
        self._save_settings_to_config()
        self._stamp_session_budget(profiles, minutes=max_minutes)

        for pid in profiles:
            card = self.profile_cards.get(pid)
            if card:
                card["status"] = "waiting"
                card["error_count"] = 0
                self._update_profile_status(pid, "waiting")
                self._update_activity(pid, "")
        self.stats = {"total": len(profiles), "running": 0, "completed": 0, "failed": 0}
        self._run_end_announced = False
        self._refresh_stats()

        config_data = self._build_engine_config(profiles)
        try:
            self.engine = WarmupEngine(
                config=config_data,
                on_status=self._on_engine_status, on_log=self._on_engine_log,
                on_activity=self._on_engine_activity, on_error=self._on_engine_error,
                on_notify=self._on_engine_notify,
            )
            self.engine.start_site_warmup(profiles, target_url,
                                          deep_links=deep_links, max_minutes=max_minutes)
        except Exception as e:
            self._add_log("APP", f"Failed to start site warmup: {e}")
            dpg.configure_item("start_btn", enabled=True)
            dpg.configure_item("site_warmup_btn", enabled=True)
            return

        dpg.configure_item("start_btn", enabled=False)
        dpg.configure_item("site_warmup_btn", enabled=False)
        dpg.configure_item("stop_btn", enabled=True)
        dpg.configure_item("resume_btn", show=False)
        self._start_elapsed_timer(max_minutes)
        self._add_log("APP", f"Site warmup started: {target_url} | {deep_links} deep links | "
                             f"{max_minutes}m max | {len(profiles)} profiles")

    def _resume_warmup(self, sender=None, app_data=None, user_data=None):
        if self.engine and self.engine.is_running():
            return
        self._save_settings_to_config()
        from core.session_store import ProgressStore
        _ps = ProgressStore()
        remaining = _ps.get_remaining_profiles()
        if not remaining:
            self._add_log("APP", "No incomplete run to resume")
            return
        config_data = self._build_engine_config(remaining)
        try:
            self.engine = WarmupEngine(
                config=config_data,
                on_status=self._on_engine_status, on_log=self._on_engine_log,
                on_activity=self._on_engine_activity, on_error=self._on_engine_error,
                on_notify=self._on_engine_notify,
            )
        except Exception as e:
            self._add_log("APP", f"Failed to create engine: {e}")
            return
        remaining = self.engine.get_remaining_profiles()
        if remaining:
            self._rebuild_profile_table(remaining)
            self.stats = {"total": len(remaining), "running": 0, "completed": 0, "failed": 0}
            self._run_end_announced = False
            self._refresh_stats()
            self._stamp_session_budget(remaining)
            self.engine.start(remaining, resume=True)
            dpg.configure_item("start_btn", enabled=True)
            dpg.configure_item("site_warmup_btn", enabled=False)
            dpg.configure_item("stop_btn", enabled=True)
            dpg.configure_item("resume_btn", show=False)
            self._start_elapsed_timer()
            self._add_log("APP", f"Resuming warmup for {len(remaining)} remaining profiles")
        else:
            self._add_log("APP", "No incomplete run to resume")

    def _test_connection(self, sender=None, app_data=None, user_data=None):
        url = self.config.get("adspower_url", _DEFAULT_API_URL)
        dpg.configure_item("test_btn", enabled=False, label="Testing...")
        dpg.configure_item("connection_label", default_value="", color=[156, 163, 175])

        def _run():
            loop = asyncio.new_event_loop()
            mgr = BrowserManager(url)
            try:
                result = loop.run_until_complete(mgr.test_connection())
            except Exception:
                result = False
            finally:
                loop.close()
            self.msg_queue.put({"type": "connection_test", "result": result})
        threading.Thread(target=_run, daemon=True).start()

    def _auto_refresh_profiles(self):
        url = self.config.get("adspower_url", _DEFAULT_API_URL)
        self._add_log("APP", "Auto-refreshing profiles from AdsPower...")

        def _run():
            loop = asyncio.new_event_loop()
            mgr = BrowserManager(url)
            try:
                profiles = loop.run_until_complete(mgr.list_profiles())
            except Exception as e:
                self.msg_queue.put({"type": "fetch_profiles", "profiles": [], "error": str(e)})
                return
            finally:
                loop.close()
            self.msg_queue.put({"type": "fetch_profiles", "profiles": profiles, "error": None})
        threading.Thread(target=_run, daemon=True).start()

    def _fetch_profiles(self, sender=None, app_data=None, user_data=None):
        url = self.config.get("adspower_url", _DEFAULT_API_URL)
        dpg.configure_item("fetch_btn", enabled=False, label="Fetching...")

        def _run():
            loop = asyncio.new_event_loop()
            mgr = BrowserManager(url)
            try:
                profiles = loop.run_until_complete(mgr.list_profiles())
            except Exception as e:
                self.msg_queue.put({"type": "fetch_profiles", "profiles": [], "error": str(e)})
                return
            finally:
                loop.close()
            self.msg_queue.put({"type": "fetch_profiles", "profiles": profiles, "error": None})
        threading.Thread(target=_run, daemon=True).start()

    def _remove_selected(self, sender=None, app_data=None, user_data=None):
        selected = self._get_selected_profiles()
        if not selected:
            self._add_log("APP", "No profiles selected to remove.")
            return
        if self.engine and self.engine.is_running():
            self._add_log("APP", "Cannot remove profiles while warmup is running.")
            return
        remaining = [pid for pid in self.profile_cards if pid not in selected]
        names = []
        for pid in selected:
            info = self.profile_info.get(pid, {})
            names.append(info.get("name", pid))
            self.profile_info.pop(pid, None)
        self.config.set("profiles", remaining)
        self.config.set("profile_info", self.profile_info)
        self.config.save()
        self._rebuild_profile_table(remaining)
        self._add_log("APP", f"Removed {len(selected)} profile(s): {', '.join(names)}")

    def _check_proxies(self, sender=None, app_data=None, user_data=None):
        profiles = self._get_selected_profiles()
        if not profiles:
            self._add_log("APP", "No profiles selected.")
            return
        missing = [p for p in profiles if not self.profile_info.get(p, {}).get("proxy")]
        if missing:
            self._add_log("APP", "Fetch profiles from AdsPower first to get proxy data.")
            return
        for pid in profiles:
            self._update_profile_status(pid, "proxy_check")
            self._update_activity(pid, "Testing proxy...")
        dpg.configure_item("proxy_btn", enabled=False, label="Checking...")
        dpg.configure_item("start_btn", enabled=False)
        self._add_log("APP", f"Fast proxy check started for {len(profiles)} profiles")

        def _run():
            loop = asyncio.new_event_loop()
            try:
                results = loop.run_until_complete(self._fast_proxy_check(profiles))
            except Exception as e:
                self.msg_queue.put({"type": "proxy_check_done", "results": [], "error": str(e)})
                return
            finally:
                loop.close()
            self.msg_queue.put({"type": "proxy_check_done", "results": results, "error": None})
        threading.Thread(target=_run, daemon=True).start()

    async def _fast_proxy_check(self, profile_ids: list) -> list:
        tasks = []
        for pid in profile_ids:
            proxy_cfg = self.profile_info.get(pid, {}).get("proxy", {})
            tasks.append(BrowserManager.check_proxy_direct(pid, proxy_cfg))
        return await asyncio.gather(*tasks)

    def _on_proxy_check_single(self, sender, app_data, user_data):
        pid = user_data
        proxy_cfg = self.profile_info.get(pid, {}).get("proxy", {})
        if not proxy_cfg or not proxy_cfg.get("proxy_host"):
            self._update_proxy_result(pid, False, error="No proxy data")
            self._add_log(pid, "Proxy check failed \u2014 no proxy configured.")
            return
        dpg.configure_item(f"{pid}_proxy_btn", enabled=False)
        if dpg.does_item_exist(f"{pid}_proxy_ip"):
            dpg.configure_item(f"{pid}_proxy_ip", default_value="checking...",
                               color=[167, 139, 250, 255])

        def _run():
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    BrowserManager.check_proxy_direct(pid, proxy_cfg))
            except Exception as e:
                result = {"profile_id": pid, "ok": False, "ip": None, "error": str(e)}
            finally:
                loop.close()
            self.msg_queue.put({"type": "proxy_check_single", "result": result})
        threading.Thread(target=_run, daemon=True).start()

    # ── UI callbacks ──────────────────────────────────────────────

    def _apply_log_height(self, h: int):
        """Set the log panel to h pixels and adjust the profile area to fill the rest."""
        min_h = 40
        max_h = 600
        h = max(min_h, min(max_h, h))
        self._log_height = h
        self._log_expanded = True
        # profile_area uses negative height = "fill minus N px from bottom"
        # overhead = log_content(h) + log_header(~22) + splitter(8) + log_header_row(~22) + padding(~20)
        overhead = h + 72
        dpg.configure_item("log_content", show=True, height=h)
        dpg.configure_item("profile_area", height=-overhead)

    def _log_toggle_visibility(self, sender=None, app_data=None, user_data=None):
        if self._log_expanded:
            dpg.configure_item("log_content", show=False)
            dpg.configure_item("profile_area", height=-50)
            self._log_expanded = False
        else:
            self._apply_log_height(self._log_height)

    # ── Splitter drag callbacks ───────────────────────────────────
    def _splitter_hovered(self, sender=None, app_data=None, user_data=None):
        self._splitter_hover = True

    def _splitter_tick(self):
        """Called every frame from run() to process drag state."""
        mouse_down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        mouse_y = dpg.get_mouse_pos(local=False)[1]

        if self._splitter_hover and mouse_down and not self._splitter_dragging:
            self._splitter_dragging = True
            self._splitter_drag_start_y = mouse_y
            self._splitter_drag_start_log_h = self._log_height if self._log_expanded else 40

        if self._splitter_dragging:
            if mouse_down:
                delta = self._splitter_drag_start_y - mouse_y  # drag up = larger log
                new_h = self._splitter_drag_start_log_h + int(delta)
                self._apply_log_height(new_h)
                if not self._log_expanded:
                    self._log_expanded = True
                    dpg.configure_item("log_content", show=True)
            else:
                self._splitter_dragging = False

        # Highlight handle on hover or drag
        active = self._splitter_hover or self._splitter_dragging
        theme = self._splitter_theme_hover if active else self._splitter_theme_normal
        dpg.bind_item_theme("splitter_handle", theme)
        dot_color = [147, 197, 253] if active else [75, 85, 99]
        dpg.configure_item("splitter_dots", color=dot_color)

        self._splitter_hover = False  # reset each frame; hover handler sets it back

    def _clear_log(self, sender=None, app_data=None, user_data=None):
        dpg.delete_item("log_content", children_only=True)
        self._log_count = 0

    def _on_session_minutes_change(self, sender, app_data, user_data):
        try:
            mins = max(15, min(120, int(app_data)))
        except (TypeError, ValueError):
            mins = 45
        if dpg.does_item_exist("session_mins_val"):
            dpg.set_value("session_mins_val", self._session_scale_caption(mins))

    def _session_scale_caption(self, minutes: int) -> str:
        minutes = max(15, min(120, int(minutes)))
        frac = (minutes - 15) / 105.0
        sites_mid = int(round(15 + frac * 25))
        depth_mid = int(round(3 + frac * 12))
        return f"{minutes} min  ·  ~{sites_mid} sites  ·  ~{depth_mid} pages/site"

    def _stamp_session_budget(self, profile_ids, minutes=None):
        if minutes is None:
            if dpg.does_item_exist("session_mins_slider"):
                minutes = int(dpg.get_value("session_mins_slider") or 45)
            else:
                minutes = int(self.config.get("session_minutes", 45) or 45)
        seconds = max(15, min(120, int(minutes))) * 60
        self._session_budget_s = seconds
        for pid in profile_ids:
            card = self.profile_cards.get(pid)
            if card:
                card["session_seconds"] = seconds
                card["run_start"] = None
                card["activity"] = ""

    def _fmt_mmss(self, seconds) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _card_countdown(self, pid: str) -> str:
        card = self.profile_cards.get(pid) or {}
        start = card.get("run_start")
        budget = card.get("session_seconds") or self._session_budget_s
        if start is None:
            return ""
        left = budget - (time.monotonic() - start)
        return self._fmt_mmss(left)

    def _on_persona_change(self, sender, app_data, user_data):
        value = app_data
        self.config.set("persona_mode", value)
        self.config.save()
        hint = self._persona_hints.get(value, "Custom search context...")
        dpg.configure_item("persona_custom_entry", hint=hint)
        self._update_persona_fields(value)

    def _update_persona_fields(self, persona_name: str):
        dpg.configure_item("persona_custom_label", show=True,
                           default_value=self._persona_labels.get(
                               persona_name, "Custom focus"))
        dpg.configure_item("persona_custom_entry", show=True)

    def _apply_persona_to_all(self, sender=None, app_data=None, user_data=None):
        value = dpg.get_value("persona_combo")
        for pid in self.profile_cards:
            if dpg.does_item_exist(f"{pid}_persona"):
                dpg.set_value(f"{pid}_persona", value)
            self.profile_info.setdefault(pid, {})["persona"] = value
        self.config.set("profile_info", self.profile_info)
        self.config.set("profile_personas", self._get_profile_personas())
        self.config.save()
        self._update_persona_fields(value)
        count = len(self.profile_cards)
        if count:
            self._add_log("APP", f"Applied '{value}' persona to {count} profiles")

    # ── Save settings ─────────────────────────────────────────────

    def _save_settings_to_config(self):
        workers = dpg.get_value("worker_input")
        workers = max(1, min(20, workers))
        self.config.set("max_concurrent", workers)
        self.config.set("persona_mode", dpg.get_value("persona_combo"))
        self.config.set("persona_custom_text", dpg.get_value("persona_custom_entry").strip())

        mins = 45
        if dpg.does_item_exist("session_mins_slider"):
            try:
                mins = int(dpg.get_value("session_mins_slider") or 45)
            except (TypeError, ValueError):
                mins = 45
        mins = max(15, min(120, mins))
        self.config.set("session_minutes", mins)

        profile_personas = self._get_profile_personas()
        self.config.set("profile_personas", profile_personas)
        for pid, persona in profile_personas.items():
            if pid in self.profile_info:
                self.profile_info[pid]["persona"] = persona
        self.config.set("profile_info", self.profile_info)

        self.config.set("target_website", dpg.get_value("target_entry").strip())
        self.config.set("target_warmup_enabled", dpg.get_value("target_enable"))
        self.config.set("youtube_enabled", dpg.get_value("youtube_cb"))
        self.config.set("bandwidth_saver", dpg.get_value("bw_saver_cb"))
        self.config.set("captcha_enabled", True)
        self.config.set("captcha_service", dpg.get_value("captcha_service"))
        self.config.set("captcha_api_key", dpg.get_value("captcha_key").strip())
        self.config.set("windows_notifications_enabled", dpg.get_value("win_notify"))
        self.config.save()

    # ── Queue polling ─────────────────────────────────────────────

    _POLL_BUDGET = 15

    def _poll_queue(self):
        processed = 0
        try:
            while processed < self._POLL_BUDGET:
                msg = self.msg_queue.get_nowait()
                msg_type = msg.get("type")
                if msg_type == "status":
                    self._handle_status_update(msg["profile_id"], msg["status"])
                elif msg_type == "log":
                    self._add_log(msg["profile_id"], msg["message"])
                elif msg_type == "activity":
                    self._update_activity(msg["profile_id"], msg["text"])
                elif msg_type == "error":
                    self._update_error(msg["profile_id"], msg["text"])
                elif msg_type == "connection_test":
                    self._handle_connection_result(msg["result"])
                elif msg_type == "fetch_profiles":
                    self._handle_fetched_profiles(msg["profiles"], msg.get("error"))
                elif msg_type == "proxy_check_done":
                    self._handle_proxy_check_done(msg["results"], msg.get("error"))
                elif msg_type == "proxy_check_single":
                    self._handle_proxy_check_single(msg["result"])
                elif msg_type == "notify":
                    self._handle_engine_notify(msg["profile_id"], msg["event_type"],
                                               msg.get("detail", ""))
                processed += 1
        except queue.Empty:
            pass

        if self.engine and not self.engine.is_running():
            dpg.configure_item("start_btn", enabled=True)
            dpg.configure_item("site_warmup_btn", enabled=True)
            dpg.configure_item("stop_btn", enabled=False)
            dpg.configure_item("proxy_btn", enabled=True, label="Check Proxies")
            self._warmup_start_time = None

            if self.engine.has_incomplete_run():
                dpg.configure_item("resume_btn", show=True)
            else:
                dpg.configure_item("resume_btn", show=False)
            self.engine = None

    def _handle_status_update(self, profile_id: str, status: str):
        self._update_profile_status(profile_id, status)

        running = sum(1 for c in self.profile_cards.values()
                      if c["status"] in ("starting", "phase1", "phase2", "phase3",
                                         "targeted", "proxy_check", "paused"))
        completed = sum(1 for c in self.profile_cards.values() if c["status"] == "completed")
        failed = sum(1 for c in self.profile_cards.values()
                     if c["status"] in ("failed", "stopped"))
        self._update_stats(running=running, completed=completed, failed=failed)

        win_notify = dpg.get_value("win_notify") if dpg.does_item_exist("win_notify") else False
        if status not in ("failed", "completed", "stopped"):
            return

        total = self.stats.get("total", 0)
        done = completed + failed
        run_over = running == 0 and total > 0 and done >= total

        if run_over and not self._run_end_announced:
            self._run_end_announced = True
            if failed and completed == 0:
                banner = f"UNSUCCESSFUL — {failed} failed"
                level = "error"
            elif failed:
                banner = f"FINISHED WITH ERRORS — {completed} completed, {failed} failed"
                level = "error"
            else:
                banner = f"ALL DONE — {completed} completed"
                level = "success"
            self._show_alert(banner, level)
            dpg.configure_item("start_btn", enabled=True)
            dpg.configure_item("site_warmup_btn", enabled=True)
            dpg.configure_item("stop_btn", enabled=False)
            dpg.configure_item("proxy_btn", enabled=True, label="Check Proxies")
            if win_notify:
                notify_all_done(completed, failed)
            return

        if status == "failed":
            self._show_alert(f"Profile #{profile_id} FAILED! ({failed} total failures)", "error")
            if win_notify:
                notify_profile_failed(profile_id, failed)
        elif status == "completed" and win_notify:
            notify_profile_completed(profile_id)

    def _handle_connection_result(self, result: bool):
        dpg.configure_item("test_btn", enabled=True, label="Test Connection")
        if result:
            dpg.configure_item("connection_label", default_value="Connected!",
                               color=[16, 185, 129])
        else:
            dpg.configure_item("connection_label", default_value="Connection failed",
                               color=[239, 68, 68])

    def _handle_fetched_profiles(self, profiles: list, error):
        dpg.configure_item("fetch_btn", enabled=True, label="Fetch Profiles from AdsPower")
        if error:
            self._add_log("APP", f"Failed to fetch profiles: {error}")
            return
        if not profiles:
            self._add_log("APP", "No profiles found in AdsPower")
            return

        profile_ids = []
        for p in profiles:
            serial = p.get("serial_number", "")
            name = p.get("name", "")
            country = p.get("ip_country", "")
            remark = p.get("remark", "")
            proxy_cfg = p.get("user_proxy_config", {})
            uid = p.get("user_id") or ""
            pid = serial if serial else uid or "unknown"
            profile_ids.append(pid)
            existing = self.profile_info.get(pid, {})
            if not uid:
                uid = existing.get("user_id") or ""
            existing.update({"name": name, "country": country, "remark": remark,
                             "proxy": proxy_cfg, "user_id": uid})
            self.profile_info[pid] = existing
            self._add_log("APP", f"  {name or pid} ({country.upper() or '??'})")

        old_ids = set(self.config.get("profiles", []))
        new_ids = set(profile_ids)
        deleted = old_ids - new_ids
        if deleted and self.engine and self.engine.is_running():
            for dpid in deleted:
                self.engine.mark_profile_deleted(dpid)
            self._add_log("APP", f"{len(deleted)} profiles deleted in AdsPower")

        self.config.set("profiles", profile_ids)
        self.config.set("profile_info", self.profile_info)
        self.config.save()

        if not (self.engine and self.engine.is_running()):
            self._rebuild_profile_table(profile_ids)
        self._add_log("APP", f"Fetched {len(profile_ids)} profiles from AdsPower")

    def _handle_proxy_check_done(self, results: list, error):
        dpg.configure_item("proxy_btn", enabled=True, label="Check Proxies")
        dpg.configure_item("start_btn", enabled=True)
        dpg.configure_item("site_warmup_btn", enabled=True)
        if error:
            self._add_log("APP", f"Proxy check failed: {error}")
            return
        ok_count = fail_count = 0
        for r in results:
            pid = r.get("profile_id", "")
            if r.get("ok"):
                ok_count += 1
                ip = r.get("ip", "?")
                self._update_profile_status(pid, "proxy_ok")
                self._update_proxy_result(pid, True, ip=ip)
                self._add_log(pid, f"Proxy OK - IP: {ip}")
            else:
                fail_count += 1
                err = r.get("error", "Unknown error")
                self._update_profile_status(pid, "proxy_fail")
                self._update_proxy_result(pid, False, error=err)
                self._add_log(pid, f"Proxy FAIL - {err}")
        self._add_log("APP", f"Proxy check done: {ok_count} OK, {fail_count} failed")

    def _handle_proxy_check_single(self, result: dict):
        pid = result.get("profile_id", "")
        if result.get("ok"):
            ip = result.get("ip", "?")
            self._update_proxy_result(pid, True, ip=ip)
            self._add_log(pid, f"Proxy OK \u2014 IP: {ip}")
        else:
            err = result.get("error", "Unknown error")
            self._update_proxy_result(pid, False, error=err)
            self._add_log(pid, f"Proxy FAILED \u2014 {err}")

    def _handle_engine_notify(self, profile_id: str, event_type: str, detail: str):
        win_notify = dpg.get_value("win_notify") if dpg.does_item_exist("win_notify") else False
        if event_type == "captcha_required":
            self._add_log(profile_id, "[CAPTCHA] Manual Google Sorry \u2014 solve in browser",
                          level="captcha")
            self._show_alert(f"Profile {profile_id}: Manual CAPTCHA required!", "warning")
            if win_notify:
                notify_captcha_required(profile_id)
        elif event_type == "captcha_resolved":
            self._add_log(profile_id, "[CAPTCHA] Solved \u2014 warmup resumed", level="success")
            if win_notify:
                notify_captcha_resolved(profile_id)
        elif event_type == "cloudflare_captcha":
            short_url = detail[:60] if detail else "unknown site"
            self._add_log(profile_id, f"[CAPTCHA] Cloudflare challenge on {short_url}",
                          level="captcha")
            self._show_alert(f"Profile {profile_id}: Cloudflare challenge", "warning")
        elif event_type == "internet_error":
            if "|" in detail:
                err_url, err_code = detail.split("|", 1)
            else:
                err_url, err_code = "", detail
            self._add_log(profile_id, f"[NETWORK] {err_url[:60]} \u2014 {err_code[:100]}",
                          level="error")
            self._show_alert(f"Profile {profile_id}: {err_url[:60]} unreachable", "error")
        elif event_type == "internet_paused":
            self._add_log("APP", f"[NETWORK] Internet lost \u2014 {detail} profile(s) paused",
                          level="warning")
            self._show_alert(f"No internet \u2014 {detail} profile(s) paused", "warning")
        elif event_type == "internet_resumed":
            self._add_log("APP", f"[NETWORK] Internet restored \u2014 {detail} profile(s) resumed",
                          level="success")
            self._show_alert(f"Internet restored \u2014 {detail} profile(s) resumed!", "success")

    # ── Logging ───────────────────────────────────────────────────

    def _add_log(self, source: str, message: str, level: str = "info",
                 action: str = "", error_code: str = ""):
        timestamp = datetime.now().strftime("%H:%M")
        extra = {"profile_id": source, "action": action, "error_code": error_code}
        if level == "error" or error_code:
            logger.error(f"[{source}] {message}", extra=extra)
        else:
            logger.info(f"[{source}] {message}", extra=extra)

        level = _detect_log_level(message, level)
        prefix_map = {"success": "OK ", "error": "ERR ", "warning": "! ",
                       "action": "-> ", "captcha": "CAPT ", "info": ""}
        prefix = prefix_map.get(level, "")
        # Compact: "HH:MM src: message"  (no brackets around timestamp/prefix)
        src = source[:12]   # cap source length to keep lines short
        line = f"{timestamp} {src}: {prefix}{message}"
        color = LOG_COLORS.get(level, LOG_COLORS["info"])

        if not dpg.does_item_exist("log_content"):
            return
        t = dpg.add_text(line, parent="log_content", color=color, wrap=0)
        if self._font_log:
            dpg.bind_item_font(t, self._font_log)
        self._log_count += 1

        if self._log_count > 800:
            children = dpg.get_item_children("log_content", 1)
            if children and len(children) > 800:
                for old in children[:len(children) - 800]:
                    if dpg.does_item_exist(old):
                        dpg.delete_item(old)

        try:
            dpg.set_y_scroll("log_content", dpg.get_y_scroll_max("log_content"))
        except Exception:
            pass

    # ── Stats & Timer ─────────────────────────────────────────────

    def _update_stats(self, total=None, running=None, completed=None, failed=None):
        if total is not None:
            self.stats["total"] = total
        if running is not None:
            self.stats["running"] = running
        if completed is not None:
            self.stats["completed"] = completed
        if failed is not None:
            self.stats["failed"] = failed
        self._refresh_stats()

    def _refresh_stats(self):
        for key in ("total", "running", "completed", "failed"):
            tag = f"stat_{key}"
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, str(self.stats.get(key, 0)))

    def _start_elapsed_timer(self, budget_minutes=None):
        self._warmup_start_time = time.monotonic()
        if budget_minutes is None:
            if dpg.does_item_exist("session_mins_slider"):
                budget_minutes = int(dpg.get_value("session_mins_slider") or 45)
            else:
                budget_minutes = int(self.config.get("session_minutes", 45) or 45)
        budget_minutes = max(15, min(120, int(budget_minutes)))
        self._session_budget_s = budget_minutes * 60
        dpg.set_value("est_time", f"{budget_minutes} min")
        dpg.set_value("elapsed", f"0:00  left {self._fmt_mmss(self._session_budget_s)}")
        self._last_timer_tick = time.monotonic()

    def _tick_timer(self):
        if self._warmup_start_time is None:
            return
        elapsed = time.monotonic() - self._warmup_start_time
        left = self._session_budget_s - elapsed
        dpg.set_value("elapsed", f"{self._fmt_mmss(elapsed)}  left {self._fmt_mmss(left)}")
        active = {"phase1", "phase2", "phase3", "targeted", "starting", "paused"}
        for pid, card in self.profile_cards.items():
            if card.get("status") in active:
                self._refresh_activity_display(pid, card.get("activity") or "")

    # ── Alert banner ──────────────────────────────────────────────

    def _show_alert(self, text: str, level: str = "error"):
        themes = {"error": self._alert_error_bg, "warning": self._alert_warn_bg,
                  "success": self._alert_ok_bg}
        icons = {"error": "(!)", "warning": "(!)", "success": "(OK)"}
        icon_colors = {"error": [252, 165, 165], "warning": [252, 211, 77],
                       "success": [110, 231, 183]}
        text_colors = {"error": [254, 226, 226], "warning": [254, 243, 199],
                       "success": [209, 250, 229]}

        dpg.bind_item_theme("alert_banner", themes.get(level, themes["error"]))
        dpg.configure_item("alert_icon", default_value=icons.get(level, "(!)"),
                           color=icon_colors.get(level, icon_colors["error"]))
        dpg.configure_item("alert_text", default_value=text,
                           color=text_colors.get(level, text_colors["error"]))
        dpg.configure_item("alert_banner", show=True)
        self._alert_visible = True
        self._alert_flash_count = 0
        self._alert_flash_time = time.monotonic()
        self._alert_level = level

    def _dismiss_alert(self, sender=None, app_data=None, user_data=None):
        dpg.configure_item("alert_banner", show=False)
        self._alert_visible = False
        dpg.set_viewport_title("AdsPower Warmup Manager v4.0.1")

    def _tick_alert_flash(self):
        if not self._alert_visible or self._alert_flash_count >= 10:
            return
        now = time.monotonic()
        if now - self._alert_flash_time < 0.5:
            return
        self._alert_flash_time = now
        self._alert_flash_count += 1
        if self._alert_flash_count % 2 == 0:
            dpg.set_viewport_title("!! ATTENTION \u2014 AdsPower Warmup Manager v4.0.1")
        else:
            dpg.set_viewport_title("AdsPower Warmup Manager v4.0.1")

    # ── Resume check ──────────────────────────────────────────────

    def _check_resume_available(self):
        from core.session_store import ProgressStore
        ps = ProgressStore()
        if ps.has_incomplete_run():
            remaining = ps.get_remaining_profiles()
            if remaining:
                dpg.configure_item("resume_btn", show=True)
                self._add_log("APP", f"Found incomplete run: {len(remaining)} profiles remaining. "
                                     "Click 'Resume' to continue.")

    # ── Cleanup ───────────────────────────────────────────────────

    def _cleanup(self):
        if self.engine and self.engine.is_running():
            try:
                self.engine.stop()
            except Exception:
                pass

    # ── Main render loop ──────────────────────────────────────────

    def run(self):
        while dpg.is_dearpygui_running():
            self._poll_queue()

            now = time.monotonic()
            if self._warmup_start_time and now - self._last_timer_tick >= 1.0:
                self._tick_timer()
                self._last_timer_tick = now

            self._tick_alert_flash()
            self._splitter_tick()

            due = [(t, cb) for t, cb in self._scheduled if t <= now]
            for t, cb in due:
                try:
                    cb()
                except Exception as e:
                    logger.error(f"Scheduled task error: {e}")
                self._scheduled.remove((t, cb))

            dpg.render_dearpygui_frame()

        self._cleanup()
        dpg.destroy_context()


if __name__ == "__main__":
    app = App()
    app.run()
