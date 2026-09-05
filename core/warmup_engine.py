"""
Warmup orchestration engine — manages adaptive warmup pipeline.

Improvements over v1:
- #1  Google click-through targets persona domains (referrer chain)
- #2  Natural tab lifecycle (open AND close tabs)
- #3  Active idle phase (browses new sites, YouTube, searches during idle)
- #4  YouTube watching integrated into phases
- #5  Auto-retry failed profiles (1-2 retries with backoff)
- #6  Session shape variation (5 distinct patterns, not rigid Phase1→2→3)
- #7  Address bar navigation mixed in for realism
- #8  Form interaction on pages (dropdowns, filters, site search)
- #9  Referrer chain building (multi-hop navigation)
- #10 Phase 2 visits 8-15 sites instead of 3-6
"""

import asyncio
import random
import logging
import time
import threading
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Dict
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .browser_manager import BrowserManager, BrowserManagerError
from .captcha_solver import CaptchaSolver
from .human_sim import HumanSimulator, _StopRequested, _SkipPhase
from .personas import assign_persona
from .session_store import SessionMemory, ProgressStore, ProfileMetrics
from . import notifications

logger = logging.getLogger(__name__)


class _TimeLimit(Exception):
    """Session minute budget exhausted — wrap up as completed."""
    pass

# ── Session shape templates (#6) ─────────────────────────────────
# Each profile gets a randomly chosen session pattern so they
# don't all look like they came from the same bot.

SESSION_SHAPES = [
    # Short timer — ramp + recon only, no idle
    {
        "name": "short",
        "phases": ["ramp", "recon"],
        "description": "Short timer — ramp + recon only, no idle",
    },
    # Classic 3-phase (existing behavior)
    {
        "name": "classic",
        "phases": ["ramp", "recon", "idle"],
        "description": "Multi-tab ramp → deep recon → long idle",
    },
    # Power browser — lots of browsing, short idle
    {
        "name": "power_browser",
        "phases": ["ramp", "recon", "youtube", "recon", "short_idle"],
        "description": "Heavy browsing with YouTube, short idle",
    },
    # Casual surfer — light browsing with long idle gaps
    {
        "name": "casual_surfer",
        "phases": ["ramp", "idle", "recon", "idle"],
        "description": "Light browsing with idle breaks between",
    },
    # Search-focused — mostly Google searches
    {
        "name": "search_focused",
        "phases": ["search_burst", "recon", "youtube", "idle"],
        "description": "Heavy searching, then exploring results",
    },
    # Quick session — short and focused
    {
        "name": "quick_session",
        "phases": ["ramp", "recon", "short_idle"],
        "description": "Short focused session, minimal idle",
    },
]


class SearchGate:
    """Short cross-profile stagger for Google search STARTS.

    A 2–8s gap avoids a simultaneous burst. It does not wait for another
    profile's 2Captcha poll, so many profiles can sit on Sorry and solve
    in parallel.
    """

    def __init__(self, min_gap: float = 2.0, max_gap: float = 8.0,
                 max_in_flight: int = 20):
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._min = min_gap
        self._max = max_gap
        _ = max_in_flight  # workers already cap in-flight searches

    async def wait_for_slot(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + random.uniform(self._min, self._max)
            wait_s = start - now
        if wait_s > 0:
            await asyncio.sleep(wait_s)


class ProfileStatus(str, Enum):
    WAITING = "waiting"
    STARTING = "starting"
    PROXY_CHECK = "proxy_check"
    PHASE1 = "phase1"
    PHASE2 = "phase2"
    PHASE3 = "phase3"
    TARGETED = "targeted"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class WarmupEngine:
    """
    Orchestrates warmup for multiple AdsPower profiles.

    Runs in a background thread with its own asyncio event loop.
    Communicates back to the UI via callback functions.
    """

    MAX_RETRIES = 2        # (#5) Auto-retry failed profiles
    RETRY_BACKOFF = [30, 90]  # Seconds to wait before each retry

    def __init__(
        self,
        config: dict,
        on_status: Callable[[str, str], None],
        on_log: Callable[[str, str], None],
        on_activity: Callable[[str, str], None] = None,
        on_error: Callable[[str, str], None] = None,
        on_notify: Callable[[str, str, str], None] = None,
    ):
        self.config = config
        self._on_status = on_status
        self._on_log = on_log
        self._on_activity = on_activity  # (profile_id, activity_text)
        self._on_error = on_error        # (profile_id, error_text)
        self._on_notify = on_notify      # (profile_id, event_type, detail)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Core components
        self.browser_mgr = BrowserManager(
            base_url=config.get("adspower_url", "http://local.adspower.net:50325"),
            retries=3,
            retry_delay=5.0,
        )
        # CAPTCHA solver (shared across all profiles) — always active
        self._captcha_solver = CaptchaSolver(
            service=config.get("captcha_service", "2captcha"),
            api_key=config.get("captcha_api_key", ""),
        )
        if self._captcha_solver.is_configured:
            logger.info(
                f"CAPTCHA solver ready: {self._captcha_solver.service} (API v2)"
            )
        else:
            logger.warning(
                "CAPTCHA solver not configured — Google Sorry will skip Google "
                "and continue via direct links"
            )

        self._human_base_timing = config.get("timing", {})

        # Persistence systems
        self.session_memory = SessionMemory()
        self.progress_store = ProgressStore()

        # Thread lock for shared mutable state accessed from multiple threads
        self._state_lock = threading.Lock()

        # Per-profile data
        self._personas: Dict[str, dict] = {}
        self._metrics: Dict[str, ProfileMetrics] = {}
        self._humans: Dict[str, HumanSimulator] = {}  # per-profile human sim
        self._visited_hosts: Dict[str, set] = {}  # profile_id → hostnames seen this session
        self._deadlines: Dict[str, float] = {}  # profile_id → monotonic deadline
        self._skip_events: Dict[str, threading.Event] = {}  # per-profile skip signal
        self._paused: Dict[str, bool] = {}  # per-profile pause flag
        self._paused_prev_status: Dict[str, str] = {}  # status before pause
        self._stopped_profiles: set = set()  # per-profile stop flag
        self._open_browsers: Dict[str, object] = {}  # pid → browser for force-close
        self._deleted_profiles: set = set()  # profiles deleted mid-run (Goal 3)
        self._net_error_cooldowns: Dict[str, float] = {}  # profile_id → last toast ts
        self._net_error_strikes: Dict[str, int] = {}    # profile_id → consecutive errors
        self._internet_paused = False                     # global: all profiles paused for connectivity
        self._internet_check_thread: Optional[threading.Thread] = None

        # Cross-event-loop concurrency control (thread-safe, works across different asyncio loops)
        # Use threading.Semaphore instead of asyncio.Semaphore so it works across event loops
        max_concurrent = config.get("max_concurrent", 20)
        self._global_semaphore = threading.Semaphore(max_concurrent)
        self._active_profiles: set = set()  # Track which profiles are currently active

        # Short Google start stagger — captcha polls overlap across profiles
        self._search_gate = SearchGate(
            min_gap=2.0, max_gap=8.0, max_in_flight=max_concurrent,
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._profile_loops: Dict[str, asyncio.AbstractEventLoop] = {}
        self._late_profiles: set = set()
        self._late_threads: list = []

    # ── Public API ────────────────────────────────────────────────

    def start(self, profile_ids: list, resume: bool = False):
        """Start warmup in a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._metrics.clear()
        self._personas.clear()
        self._paused.clear()
        self._paused_prev_status.clear()
        self._skip_events.clear()
        self._humans.clear()
        self._stopped_profiles.clear()
        self._open_browsers.clear()
        self._deleted_profiles.clear()
        self._late_profiles.clear()
        self._late_threads.clear()
        self._profile_loops.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=(profile_ids, resume), daemon=True
        )
        self._thread.start()

    def stop(self):
        """Signal all running warmups to stop and force-close browsers."""
        self._stop_event.set()
        self._force_close_all_browsers()

    def _loop_for_profile(self, profile_id: str = None):
        if profile_id:
            loop = self._profile_loops.get(profile_id)
            if loop and loop.is_running():
                return loop
        loop = self._loop
        if loop and loop.is_running():
            return loop
        return None

    def _run_on_loop(self, loop, coro, timeout: float = 5.0):
        if not loop or not loop.is_running():
            return False
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False

    def _force_close_all_browsers(self):
        """Force-close every tracked browser on the loop that owns it."""
        browsers = list(self._open_browsers.items())
        if not browsers:
            return

        def _do_close():
            for pid, browser in browsers:
                loop = self._loop_for_profile(pid)
                if loop:
                    self._run_on_loop(
                        loop, self._close_one_browser(pid, browser), timeout=4.0
                    )
                    continue
                try:
                    asyncio.run(self.browser_mgr.stop_browser(pid))
                except Exception:
                    pass

        threading.Thread(target=_do_close, daemon=True).start()

    async def _close_one_browser(self, pid, browser):
        try:
            if browser:
                await asyncio.wait_for(browser.close(), timeout=2.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.browser_mgr.stop_browser(pid), timeout=2.0)
        except Exception:
            pass

    async def _close_all_browsers(self, browsers):
        """Close all tracked browsers concurrently with a hard 4s deadline."""
        await asyncio.wait_for(
            asyncio.gather(
                *[self._close_one_browser(pid, b) for pid, b in browsers],
                return_exceptions=True,
            ),
            timeout=4.0,
        )

    def skip_profile(self, profile_id: str):
        """Skip the current operation for a specific profile and move to the next step."""
        event = self._skip_events.get(profile_id)
        if event:
            event.set()
            self._log(profile_id, "Skip requested — moving to next step")

    def pause_profile(self, profile_id: str):
        """Pause a profile's warmup so the user can work in the browser manually."""
        with self._state_lock:
            if self._paused.get(profile_id):
                return  # Already paused
            self._paused[profile_id] = True
        self._update_status(profile_id, ProfileStatus.PAUSED)
        self._activity(profile_id, "Paused — work in browser, then Resume")
        self._log(profile_id, "Paused by user — browser stays open")

    def resume_profile(self, profile_id: str):
        """Resume a paused profile's warmup from where it stopped."""
        with self._state_lock:
            if not self._paused.get(profile_id):
                return  # Not paused
            self._paused[profile_id] = False
            prev = self._paused_prev_status.pop(profile_id, None)
        # Restore the status that was active before the pause
        if prev:
            self._update_status(profile_id, prev)
        else:
            # Fallback: set a generic "running" status
            self._update_status(profile_id, ProfileStatus.PHASE1)
        self._activity(profile_id, "Resumed — warmup continuing")
        self._log(profile_id, "Resumed by user — continuing warmup")

    def pause_profile_for_captcha(self, profile_id: str):
        """Pause a profile because of a manual Google Sorry CAPTCHA."""
        with self._state_lock:
            if self._paused.get(profile_id):
                return
            self._paused[profile_id] = True
        self._update_status(profile_id, ProfileStatus.PAUSED)
        self._activity(profile_id, "Manual CAPTCHA required — solve in browser")
        self._log(profile_id,
                  "[CAPTCHA] Profile paused for manual Google Sorry CAPTCHA")

    def resume_profile_after_captcha(self, profile_id: str):
        """Resume a profile after manual Google Sorry CAPTCHA was cleared."""
        with self._state_lock:
            if not self._paused.get(profile_id):
                return
            self._paused[profile_id] = False
            prev = self._paused_prev_status.pop(profile_id, None)
        if prev:
            self._update_status(profile_id, prev)
        else:
            self._update_status(profile_id, ProfileStatus.PHASE1)
        self._activity(profile_id, "CAPTCHA cleared — warmup resuming")
        self._log(profile_id,
                  "[CAPTCHA] CAPTCHA cleared, profile resumed")

    # ── Internet auto-pause / auto-resume ────────────────────────

    _NET_STRIKE_THRESHOLD = 3  # consecutive errors before global pause

    def _record_net_strike(self, profile_id: str):
        """Increment strike counter for a profile; trigger global pause if threshold hit."""
        with self._state_lock:
            strikes = self._net_error_strikes.get(profile_id, 0) + 1
            self._net_error_strikes[profile_id] = strikes
        if strikes >= self._NET_STRIKE_THRESHOLD and not self._internet_paused:
            self._pause_all_for_internet()

    def _clear_net_strikes(self, profile_id: str):
        """Reset strike counter (called on successful navigation)."""
        with self._state_lock:
            self._net_error_strikes.pop(profile_id, None)

    def _pause_all_for_internet(self):
        """Pause every running profile and start a connectivity checker."""
        if self._internet_paused:
            return
        self._internet_paused = True

        paused_count = 0
        with self._state_lock:
            for pid in list(self._active_profiles):
                if not self._paused.get(pid) and pid not in self._stopped_profiles:
                    self._paused[pid] = True
                    paused_count += 1
        for pid in list(self._active_profiles):
            self._update_status(pid, ProfileStatus.PAUSED)
            self._activity(pid, "⏸ Paused — no internet, waiting for connection...")

        self._log("ENGINE", f"Internet lost — {paused_count} profile(s) paused, checking connectivity...")

        notify_enabled = self.config.get("windows_notifications_enabled", True)
        if notify_enabled:
            notifications.notify_internet_paused(paused_count)
        if self._on_notify:
            try:
                self._on_notify("ENGINE", "internet_paused", str(paused_count))
            except Exception:
                pass

        if self._internet_check_thread is None or not self._internet_check_thread.is_alive():
            self._internet_check_thread = threading.Thread(
                target=self._connectivity_check_loop, daemon=True)
            self._internet_check_thread.start()

    def _connectivity_check_loop(self):
        """Poll a lightweight URL until reachable, then auto-resume all profiles."""
        import urllib.request
        probe_urls = [
            "https://www.google.com/generate_204",
            "https://clients3.google.com/generate_204",
            "http://www.gstatic.com/generate_204",
        ]
        while not self._stop_event.is_set():
            time.sleep(5)
            for url in probe_urls:
                try:
                    req = urllib.request.Request(url, method="HEAD")
                    with urllib.request.urlopen(req, timeout=8):
                        pass
                    self._resume_all_after_internet()
                    return
                except Exception:
                    continue

    def _resume_all_after_internet(self):
        """Internet is back — resume all profiles that were paused for connectivity."""
        if not self._internet_paused:
            return
        self._internet_paused = False

        resumed_count = 0
        with self._state_lock:
            self._net_error_strikes.clear()
            for pid in list(self._active_profiles):
                if self._paused.get(pid) and pid not in self._stopped_profiles:
                    self._paused[pid] = False
                    prev = self._paused_prev_status.pop(pid, None)
                    resumed_count += 1
                    if prev:
                        self._update_status(pid, prev)
                    else:
                        self._update_status(pid, ProfileStatus.PHASE1)
                    self._activity(pid, "▶ Internet restored — resuming warmup")

        self._log("ENGINE", f"Internet restored — {resumed_count} profile(s) resumed")

        notify_enabled = self.config.get("windows_notifications_enabled", True)
        if notify_enabled:
            notifications.notify_internet_resumed(resumed_count)
        if self._on_notify:
            try:
                self._on_notify("ENGINE", "internet_resumed", str(resumed_count))
            except Exception:
                pass

    def stop_single_profile(self, profile_id: str):
        """Stop a single profile without affecting others."""
        with self._state_lock:
            self._stopped_profiles.add(profile_id)
        event = self._skip_events.get(profile_id)
        if event:
            event.set()
        browser = self._open_browsers.pop(profile_id, None)
        if browser:
            def _close():
                loop = self._loop_for_profile(profile_id)
                if loop:
                    self._run_on_loop(
                        loop, self._close_one_browser(profile_id, browser), timeout=4.0
                    )
                    return
                try:
                    asyncio.run(self.browser_mgr.stop_browser(profile_id))
                except Exception:
                    pass
            threading.Thread(target=_close, daemon=True).start()
        self._log(profile_id, "Stop signal sent for this profile")

    def attach_profile_info(self, profile_id: str, info: dict = None):
        """Merge UI/config profile metadata into the running engine snapshot."""
        pid = str(profile_id)
        incoming = dict(info or {})
        store = self.config.setdefault("profile_info", {})
        existing = store.get(pid) or store.get(profile_id) or {}
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        for key, value in incoming.items():
            if value not in (None, ""):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        store[pid] = merged

    def start_single_profile(self, profile_id: str, profile_info: dict = None):
        """Add a single profile to an already-running engine.

        Uses the shared semaphore so it respects max_concurrent even when
        started individually (prevents bypassing the concurrency limit).
        """
        if profile_info:
            self.attach_profile_info(profile_id, profile_info)
        if not self.is_running():
            # Fall back to a normal start for one profile
            self.start([profile_id])
            return

        self._late_profiles.add(str(profile_id))
        t = threading.Thread(
            target=self._run_single_profile_loop,
            args=(profile_id,), daemon=True
        )
        self._late_threads.append(t)
        t.start()

    def _run_single_profile_loop(self, profile_id: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._profile_loops[profile_id] = loop
        try:
            loop.run_until_complete(self._run_single_profile(profile_id))
        except Exception as e:
            logger.error(f"Single profile warmup crashed for {profile_id}: {e}")
            m = self._metrics.get(profile_id)
            if m:
                m.status = "failed"
                if not m.finished_at:
                    m.finish()
            self._update_status(profile_id, ProfileStatus.FAILED)
        finally:
            self._profile_loops.pop(profile_id, None)
            self._late_profiles.discard(str(profile_id))
            loop.close()

    async def _run_single_profile(self, profile_id: str):
        """Run warmup for a single profile (when adding to a running engine)."""
        # Clear any previous stop flag for this profile
        self._stopped_profiles.discard(profile_id)

        # Assign persona
        all_sites = list(self.config.get("sites", []))
        all_queries = list(self.config.get("search_queries", []))
        persona_mode = self.config.get("persona_mode", "Random")
        profile_personas = self.config.get("profile_personas", {})
        per_profile = profile_personas.get(profile_id, persona_mode)
        from .personas import persona_uses_custom_text
        profile_custom_texts = self.config.get("profile_custom_texts", {})
        raw_custom = profile_custom_texts.get(profile_id, self.config.get("persona_custom_text", ""))
        custom_text = raw_custom if persona_uses_custom_text(per_profile) else ""
        persona = assign_persona(profile_id, all_sites, all_queries,
                                 forced_persona=per_profile, custom_text=custom_text)
        persona["sites"] = self._strip_youtube(persona.get("sites", []), profile_id)
        persona["queries"] = [
            q for q in persona.get("queries", [])
            if self._youtube_ok(profile_id) or "youtube" not in q.lower()
        ]
        self._personas[profile_id] = persona
        self._visited_hosts[profile_id] = set()

        shape = self._pick_session_shape(profile_id)
        persona["session_shape"] = shape
        persona["sites_budget"] = self._scale_sites(profile_id)
        persona["google_budget"] = max(int(persona["sites_budget"]), 18)
        self._log(profile_id, f"Persona: {persona['persona_name']} | Session: {shape['name']} | "
                              f"{self._session_minutes(profile_id)}m / {persona['sites_budget']} sites")

        # Register with progress store
        self.progress_store.update_profile(profile_id, "starting")

        # Use thread-safe semaphore that works across event loops
        # Create a wrapper that converts threading.Semaphore to asyncio-compatible
        try:
            await self._warmup_profile_with_retry_threadsafe(profile_id)
        except _StopRequested:
            pass
        except Exception as e:
            logger.error(f"Single profile {profile_id} failed: {e}")
            m = self._metrics.get(profile_id)
            if m:
                m.status = "failed"
                if not m.finished_at:
                    m.finish()
            self._update_status(profile_id, ProfileStatus.FAILED)

    # ── Site-Only Warmup (direct targeted warmup) ───────────────

    def start_site_warmup(self, profile_ids: list, target_url: str,
                           deep_links: int = 10, max_minutes: int = 15):
        """
        Start a site-only warmup: go straight to the target site via Google
        search and explore it deeply. No general browsing — just the target.

        Args:
            profile_ids: List of AdsPower profile IDs to warm up
            target_url: The website URL to warm up (e.g. "openrouter.ai")
            deep_links: How many internal pages to explore per visit (5-25)
            max_minutes: Maximum time allowed per profile (15-120 min)
        """
        self._stop_event.clear()
        self._stopped_profiles.clear()
        self._personas.clear()
        self._metrics.clear()
        max_minutes = max(15, min(120, int(max_minutes)))

        self._thread = threading.Thread(
            target=self._run_site_warmup_loop,
            args=(profile_ids, target_url, deep_links, max_minutes),
            daemon=True,
        )
        self._thread.start()

    def _run_site_warmup_loop(self, profile_ids, target_url, deep_links, max_minutes):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(
                self._run_site_warmup(profile_ids, target_url, deep_links, max_minutes)
            )
        except Exception as e:
            logger.error(f"Site warmup crashed: {e}")
        finally:
            self._loop = None
            loop.close()

    async def _run_site_warmup(self, profile_ids, target_url, deep_links, max_minutes):
        """Async orchestrator for site-only warmup across multiple profiles."""
        import time as _time

        max_concurrent = self.config.get("max_concurrent", 20)
        semaphore = asyncio.Semaphore(max_concurrent)
        self._shared_semaphore = semaphore

        # Normalize URL
        if not target_url.startswith("http"):
            target_url = "https://" + target_url

        tasks = []
        for pid in profile_ids:
            tasks.append(
                self._site_warmup_profile(pid, semaphore, target_url,
                                           deep_links, max_minutes)
            )

        await asyncio.gather(*tasks, return_exceptions=True)
        self._emit_run_summary(profile_ids)

    async def _site_warmup_profile(self, profile_id: str, semaphore: asyncio.Semaphore,
                                     target_url: str, deep_links: int, max_minutes: int):
        """Run site-only warmup for a single profile."""
        import time as _time

        metrics = ProfileMetrics(profile_id)
        self._metrics[profile_id] = metrics
        metrics.persona = "Site Warmup"

        human = self._get_human(profile_id)
        human.set_target_host(target_url)
        try:
            target_domain = urlparse(target_url).netloc.replace("www.", "")
        except Exception:
            target_domain = ""
        if not target_domain:
            target_domain = target_url.replace("https://", "").replace(
                "http://", "").split("/")[0].replace("www.", "")
        self._activity(profile_id, "Waiting for slot...")

        async with semaphore:
            self._update_status(profile_id, ProfileStatus.STARTING)
            self._activity(profile_id, "Starting browser...")
            self._log(profile_id, f"Site warmup: {target_url} ({deep_links} deep links, {max_minutes}m max)")

            browser = None
            deadline = _time.monotonic() + max_minutes * 60

            try:
                ws_endpoint = await self._start_or_attach_browser(profile_id)
                self._log(profile_id, f"Connected: {ws_endpoint[:60]}...")

                async with async_playwright() as pw:
                    browser = await self._connect_cdp(pw, ws_endpoint, profile_id)
                    self._open_browsers[profile_id] = browser
                    if not browser.contexts:
                        context = await browser.new_context()
                    else:
                        context = browser.contexts[0]
                    await Stealth().apply_stealth_async(context)
                    page = await self._ensure_page(context, browser)
                    human.setup_dialog_handler(page)
                    await human.sync_viewport_to_window(page)
                    await human.maybe_toggle_bookmarks_bar(page)

                    if self._pcfg(profile_id, "bandwidth_saver", False):
                        self._log(
                            profile_id,
                            "Bandwidth saver ignored on Site Warmup — images/pixels must load",
                        )

                    self._update_status(profile_id, ProfileStatus.TARGETED)
                    self._activity(profile_id, f"Opening {target_url} directly...")

                    try:
                        deep_n = max(2, int(deep_links or 4))
                    except (TypeError, ValueError):
                        deep_n = 4
                    depth_per_visit = (max(2, deep_n // 2), deep_n)
                    site_custom = self.config.get("persona_custom_text", "")

                    self._log(
                        profile_id,
                        f"Plan: open {target_url} directly, then explore up to {deep_n} pages",
                    )

                    reached = False
                    timed_out = False
                    try:
                        reached = bool(await asyncio.wait_for(
                            human.targeted_site_warmup(
                                page, context, target_url,
                                num_visits=1,
                                depth_per_visit=depth_per_visit,
                                custom_text=site_custom,
                                metrics=metrics,
                                direct_arrival=True,
                                max_pages=deep_n,
                            ),
                            timeout=max_minutes * 60,
                        ))
                    except asyncio.TimeoutError:
                        timed_out = True
                        reached = True
                        self._log(
                            profile_id,
                            f"Time limit reached ({max_minutes}m) — wrapping up",
                        )
                    except _SkipPhase:
                        self._log(profile_id, "Site warmup skipped by user")

                    try:
                        cookies = await context.cookies()
                        metrics.analyze_cookies(
                            cookies, metrics.unique_domains,
                            target_domain=target_domain,
                        )
                        self._log(profile_id, metrics.cookie_log_line())
                    except Exception:
                        pass

                    metrics.finish()
                    site_elapsed = int(metrics.duration_s / 60) if metrics.duration_s else 0
                    if reached:
                        metrics.record_page_visit(target_url)
                        self._update_status(profile_id, ProfileStatus.COMPLETED)
                        self._activity(profile_id, "Site warmup complete")
                        self._log(
                            profile_id,
                            "Site warmup finished successfully"
                            if not timed_out
                            else "Site warmup finished (time limit)",
                        )
                        metrics.status = "completed"
                        await self._write_warmup_note(
                            profile_id, "completed", site_elapsed, target_url)
                    else:
                        self._update_status(profile_id, ProfileStatus.FAILED)
                        self._activity(profile_id, "Site warmup failed — target never opened")
                        self._log(
                            profile_id,
                            f"Site warmup failed: could not open {target_url}",
                        )
                        metrics.status = "failed"
                        await self._write_warmup_note(
                            profile_id, "failed", site_elapsed, target_url)

            except _StopRequested:
                metrics.finish()
                site_elapsed = int(metrics.duration_s / 60) if metrics.duration_s else 0
                self._log(profile_id, "Site warmup stopped by user")
                self._update_status(profile_id, ProfileStatus.STOPPED)
                metrics.status = "stopped"
                try:
                    await self._write_warmup_note(
                        profile_id, "stopped", site_elapsed, target_url)
                except Exception:
                    pass
            except Exception as e:
                self._log(profile_id, f"Site warmup failed: {e}")
                self._update_status(profile_id, ProfileStatus.FAILED)
                self._error(profile_id, str(e))
                metrics.status = "failed"
                logger.error(f"Site warmup error for {profile_id}: {e}")
                try:
                    await self._write_warmup_note(
                        profile_id, "failed", 0, target_url)
                except Exception:
                    pass
            finally:
                self._open_browsers.pop(profile_id, None)
                await self._cleanup_browser(browser, profile_id)

    def is_profile_paused(self, profile_id: str) -> bool:
        return self._paused.get(profile_id, False)

    def is_running(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        return any(t.is_alive() for t in list(self._late_threads))

    def get_metrics(self) -> Dict[str, ProfileMetrics]:
        """Get metrics for all profiles. Returns a copy for thread safety."""
        return self._metrics.copy()

    def has_incomplete_run(self) -> bool:
        return self.progress_store.has_incomplete_run()

    def get_remaining_profiles(self) -> list:
        return self.progress_store.get_remaining_profiles()

    # ── Thread entry ──────────────────────────────────────────────

    def _run_loop(self, profile_ids: list, resume: bool = False):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run_all(profile_ids, resume))
        except Exception as e:
            logger.error(f"Engine crashed: {e}")
        finally:
            self._loop = None
            loop.close()

    # ── Async orchestration ───────────────────────────────────────

    async def _run_all(self, profile_ids: list, resume: bool = False):
        if not profile_ids:
            self._log("ENGINE", "No profiles to process")
            return

        max_concurrent = self.config.get("max_concurrent", 20)
        semaphore = asyncio.Semaphore(max_concurrent)
        # Store for single-profile starts to respect concurrency limit
        self._shared_semaphore = semaphore
        # Update global thread-safe semaphore to match (for cross-event-loop coordination)
        # Release all current permits and recreate with new limit
        old_sem = self._global_semaphore
        self._global_semaphore = threading.Semaphore(max_concurrent)
        # Release old semaphore permits (if any were held, they're now released)
        # Note: This is best-effort - active profiles will continue with old limit until they finish

        # Handle resume
        if resume:
            remaining = self.progress_store.get_remaining_profiles()
            if remaining:
                self._log("ENGINE", f"Resuming: {len(remaining)} profiles left")
                profile_ids = remaining
            else:
                self._log("ENGINE", "No incomplete run to resume, starting fresh")

        self.progress_store.start_run(profile_ids)

        # Log session budget
        mins = self._session_minutes()
        self._log("ENGINE", f"Session timer: {mins}m | sites 15-40 | depth 3-15 (scaled)")

        # Assign personas (per-profile if available, else global fallback)
        all_sites = list(self.config.get("sites", []))
        all_queries = list(self.config.get("search_queries", []))
        persona_mode = self.config.get("persona_mode", "Random")
        profile_personas = self.config.get("profile_personas", {})  # {pid: persona_name}

        from .personas import persona_uses_custom_text
        profile_custom_texts = self.config.get("profile_custom_texts", {})
        for pid in profile_ids:
            per_profile = profile_personas.get(pid, persona_mode)
            raw_custom = profile_custom_texts.get(pid, self.config.get("persona_custom_text", ""))
            custom_text = raw_custom if persona_uses_custom_text(per_profile) else ""
            persona = assign_persona(pid, all_sites, all_queries,
                                     forced_persona=per_profile, custom_text=custom_text)
            persona["sites"] = self._strip_youtube(persona.get("sites", []), pid)
            persona["queries"] = [
                q for q in persona.get("queries", [])
                if self._youtube_ok(pid) or "youtube" not in q.lower()
            ]
            self._personas[pid] = persona
            self._visited_hosts[pid] = set()

            shape = self._pick_session_shape(pid)
            persona["session_shape"] = shape
            persona["sites_budget"] = self._scale_sites(pid)
            google_budget = max(int(persona["sites_budget"]), 18)
            persona["google_budget"] = google_budget

            self._log(pid, f"Persona: {persona['persona_name']} "
                          f"({len(persona['sites'])} sites, {len(persona['queries'])} queries) "
                          f"| Session: {shape['name']} | {self._session_minutes(pid)}m / {persona['sites_budget']} sites")

        # Start every profile immediately. max_concurrent + AdsPower 3s start
        # spacing pace browsers; launch_batch_* is not used as a hard limiter.
        tasks = []
        for pid in profile_ids:
            if self._stop_event.is_set():
                break
            tasks.append(asyncio.create_task(
                self._warmup_profile_with_retry(pid, semaphore)
            ))
        self._log("ENGINE", f"Launched {len(tasks)} profiles in parallel "
                            f"(max {max_concurrent} concurrent)")

        await asyncio.gather(*tasks, return_exceptions=True)

        # Profiles started mid-run live on extra threads — wait so we don't
        # declare the run finished while they are still going (or failing).
        await asyncio.get_running_loop().run_in_executor(
            None, self._wait_for_late_profiles
        )
        self._emit_run_summary(profile_ids)

        all_done = all(
            pid in self._metrics and self._metrics[pid].finished_at > 0
            for pid in list(profile_ids) + list(self._metrics.keys())
        )
        if all_done:
            self.progress_store.clear()

        # Log health scores (send as score update, don't overwrite final status)
        self._log("ENGINE", "─── HEALTH SCORES ───")
        for pid, m in self._metrics.items():
            score = m.health_score()
            label = m.score_label()
            self._log(pid, f"Score: {score}/100 ({label})")
            # Use a dedicated score message that won't overwrite failed/stopped status
            try:
                self._on_status(pid, f"score:{score}")
            except Exception:
                pass

    def _wait_for_late_profiles(self):
        while not self._stop_event.is_set():
            with self._state_lock:
                late = {str(p) for p in self._late_profiles}
            alive = [t for t in list(self._late_threads) if t.is_alive()]
            self._late_threads = alive
            if not late and not alive:
                break
            time.sleep(0.4)

    def _emit_run_summary(self, profile_ids: list = None):
        """Log the real outcome from metrics — not asyncio.gather return values."""
        seen = []
        seen_set = set()
        for pid in list(profile_ids or []) + list(self._metrics.keys()):
            key = str(pid)
            if key not in seen_set:
                seen_set.add(key)
                seen.append(key)

        completed = failed = stopped = 0
        for pid in seen:
            m = self._metrics.get(pid)
            st = (m.status if m else "") or ""
            if st == "completed":
                completed += 1
            elif st == "stopped":
                stopped += 1
            elif st == "failed":
                failed += 1
            elif m and m.finished_at and not m.errors:
                completed += 1
            else:
                failed += 1

        parts = [f"{completed} completed", f"{failed} failed"]
        if stopped:
            parts.append(f"{stopped} stopped")
        if failed:
            self._log("ENGINE", f"Run unsuccessful — {', '.join(parts)}")
        elif stopped and completed == 0:
            self._log("ENGINE", f"Run stopped — {', '.join(parts)}")
        else:
            self._log("ENGINE", f"Run complete — {', '.join(parts)}")

    # ── (#5) Auto-retry wrapper ───────────────────────────────────

    async def _warmup_profile_with_retry(self, profile_id: str,
                                          semaphore: asyncio.Semaphore):
        """Wrap _warmup_profile with auto-retry on failure."""
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 2):  # attempt 1, 2, 3
            if self._stop_event.is_set():
                raise _StopRequested()
            if profile_id in self._stopped_profiles:
                raise _StopRequested()

            try:
                await self._warmup_profile(profile_id, semaphore)
                m = self._metrics.get(profile_id)
                if m and m.status == "failed":
                    raise RuntimeError("Warmup finished in failed state")
                return
            except _StopRequested:
                raise
            except Exception as e:
                last_error = e
                if attempt <= self.MAX_RETRIES:
                    backoff = self.RETRY_BACKOFF[min(attempt - 1, len(self.RETRY_BACKOFF) - 1)]
                    self._log(profile_id,
                              f"Failed (attempt {attempt}/{self.MAX_RETRIES + 1}): {e}")
                    self._log(profile_id, f"Retrying in {backoff}s...")
                    self._update_status(profile_id, ProfileStatus.WAITING)
                    try:
                        await self._cancellable_sleep(backoff)
                    except _StopRequested:
                        raise
                else:
                    self._log(profile_id,
                              f"Failed after {attempt} attempts: {last_error}")
                    m = self._metrics.get(profile_id)
                    if m:
                        m.status = "failed"
                        if not m.finished_at:
                            m.finish()
                    self._update_status(profile_id, ProfileStatus.FAILED)
                    raise last_error

    async def _warmup_profile_with_retry_threadsafe(self, profile_id: str):
        """Wrap _warmup_profile with auto-retry, using thread-safe semaphore for cross-event-loop support."""
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 2):  # attempt 1, 2, 3
            if self._stop_event.is_set():
                raise _StopRequested()
            if profile_id in self._stopped_profiles:
                raise _StopRequested()

            try:
                await self._warmup_profile_threadsafe(profile_id)
                m = self._metrics.get(profile_id)
                if m and m.status == "failed":
                    raise RuntimeError("Warmup finished in failed state")
                return
            except _StopRequested:
                raise
            except Exception as e:
                last_error = e
                if attempt <= self.MAX_RETRIES:
                    backoff = self.RETRY_BACKOFF[min(attempt - 1, len(self.RETRY_BACKOFF) - 1)]
                    self._log(profile_id,
                              f"Failed (attempt {attempt}/{self.MAX_RETRIES + 1}): {e}")
                    self._log(profile_id, f"Retrying in {backoff}s...")
                    self._update_status(profile_id, ProfileStatus.WAITING)
                    try:
                        await self._cancellable_sleep(backoff)
                    except _StopRequested:
                        raise
                else:
                    self._log(profile_id,
                              f"Failed after {attempt} attempts: {last_error}")
                    m = self._metrics.get(profile_id)
                    if m:
                        m.status = "failed"
                        if not m.finished_at:
                            m.finish()
                    self._update_status(profile_id, ProfileStatus.FAILED)
                    raise last_error

    async def _warmup_profile_threadsafe(self, profile_id: str):
        """Run the full warmup pipeline for a single profile using thread-safe semaphore."""
        # Initialize metrics BEFORE try/semaphore so exception handlers always have it
        metrics = ProfileMetrics(profile_id)
        self._metrics[profile_id] = metrics
        persona_data = self._personas.get(profile_id, {})
        metrics.persona = persona_data.get("persona_name", "Unknown")

        # Create per-profile human simulator with activity callback
        human = self._get_human(profile_id)
        self._activity(profile_id, "Waiting for slot...")

        # Acquire thread-safe semaphore (blocks if max_concurrent reached)
        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._global_semaphore.acquire)
        
        # Track this profile as active
        with self._state_lock:
            self._active_profiles.add(profile_id)
        
        try:
            # Now run the actual warmup (same as _warmup_profile but without semaphore context)
            await self._warmup_profile_core(profile_id, metrics, human, persona_data)
        finally:
            # Release semaphore and remove from active set
            with self._state_lock:
                self._active_profiles.discard(profile_id)
            self._global_semaphore.release()

    # ── Browser start with attach-on-conflict ────────────────────

    async def _start_or_attach_browser(self, profile_id: str) -> str:
        """Start a browser or attach to an already-running one (Goal 2)."""
        try:
            return await self.browser_mgr.start_browser(profile_id)
        except BrowserManagerError as e:
            err_msg = str(e).lower()
            if "already" in err_msg or "running" in err_msg or "active" in err_msg:
                self._log(profile_id, "Profile already open — attaching to existing session")
                ws = await self.browser_mgr.get_browser_ws(profile_id)
                if ws:
                    return ws
            raise

    async def _connect_cdp(self, pw, ws_endpoint: str, profile_id: str,
                           max_attempts: int = 3, base_delay: float = 5.0):
        """Connect to browser via CDP with retries and warm-up delay.

        AdsPower may return the WS URL before the browser is fully ready,
        causing 'Protocol error (Browser.getVersion): undefined'. A short
        delay + retry loop handles this reliably.
        """
        if "/session" in ws_endpoint:
            self._log(profile_id,
                      "Warning: CDP endpoint has /session (masked). Update AdsPower to latest or check cdp_mask=0.")
        await asyncio.sleep(base_delay)
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await pw.chromium.connect_over_cdp(ws_endpoint)
            except Exception as e:
                last_err = e
                if attempt < max_attempts:
                    wait = base_delay * attempt
                    self._log(profile_id,
                              f"CDP connect failed (attempt {attempt}/{max_attempts}): {e} — retrying in {wait:.0f}s")
                    await asyncio.sleep(wait)
        raise last_err

    # ── Per-profile config helper ────────────────────────────────

    def _youtube_ok(self, profile_id: str = None) -> bool:
        if profile_id:
            return bool(self._pcfg(profile_id, "youtube_enabled", True))
        return bool(self.config.get("youtube_enabled", True))

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        u = (url or "").lower()
        return "youtube." in u or "youtu.be" in u

    def _strip_youtube(self, urls: list, profile_id: str = None) -> list:
        """Drop YouTube URLs when YouTube browsing is disabled."""
        if self._youtube_ok(profile_id):
            return list(urls or [])
        return [u for u in (urls or []) if not self._is_youtube_url(u)]

    @staticmethod
    def _host_of(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw
        try:
            host = urlparse(raw).netloc.lower().replace("www.", "")
        except Exception:
            host = raw.lower()
        return host.split(":")[0]

    def _already_visited(self, profile_id: str, url: str) -> bool:
        host = self._host_of(url)
        if not host or host == "google.com":
            return False
        return host in self._visited_hosts.setdefault(profile_id, set())

    def _mark_visited(self, profile_id: str, url: str):
        host = self._host_of(url)
        if host and host != "google.com":
            self._visited_hosts.setdefault(profile_id, set()).add(host)

    def _is_session_target(self, profile_id: str, url: str) -> bool:
        """True if url is the configured target marketplace (Google-only Step 2)."""
        if not self._pcfg(profile_id, "target_warmup_enabled", False):
            return False
        target = self._pcfg(profile_id, "target_website", "") or ""
        th = self._host_of(target)
        uh = self._host_of(url)
        if not th or not uh:
            return False
        return th == uh or uh.endswith("." + th) or th.endswith("." + uh)

    def _pick_session_shape(self, profile_id: str = None) -> dict:
        minutes = self._session_minutes(profile_id)
        if minutes < 25:
            return dict(SESSION_SHAPES[0])
        pool = list(SESSION_SHAPES[1:])
        if not self._youtube_ok(profile_id):
            no_yt = [s for s in pool if "youtube" not in s.get("phases", [])]
            if no_yt:
                pool = no_yt
        return random.choice(pool)

    def _pcfg(self, profile_id: str, key: str, default=None):
        """Get a config value, checking per-profile overrides first."""
        overrides = self.config.get("profile_overrides", {})
        profile_overrides = overrides.get(profile_id, {})
        if key in profile_overrides:
            return profile_overrides[key]
        return self.config.get(key, default)

    def _session_minutes(self, profile_id: str = None) -> int:
        m = self._pcfg(profile_id, "session_minutes", 45)
        try:
            m = int(m)
        except (TypeError, ValueError):
            m = 45
        return max(15, min(120, m))

    def _set_deadline(self, profile_id: str):
        self._deadlines[profile_id] = time.monotonic() + self._session_minutes(profile_id) * 60

    def _time_left(self, profile_id: str) -> float:
        dl = self._deadlines.get(profile_id)
        if dl is None:
            return 10 ** 6
        return dl - time.monotonic()

    def _check_deadline(self, profile_id: str):
        self._check_stop(profile_id)
        if self._time_left(profile_id) <= 0:
            raise _TimeLimit()

    def _scale_sites(self, profile_id: str) -> int:
        ov = None
        overrides = self.config.get("profile_overrides", {}).get(profile_id, {})
        if "sites_per_profile" in overrides:
            try:
                ov = int(overrides["sites_per_profile"])
            except (TypeError, ValueError):
                ov = None
        if ov is not None:
            return max(15, min(40, ov))
        minutes = self._session_minutes(profile_id)
        frac = (minutes - 15) / 105.0
        mid = 15 + frac * 25
        lo = max(15, int(round(mid - 4)))
        hi = min(40, int(round(mid + 4)))
        if lo > hi:
            lo, hi = 15, 40
        return random.randint(lo, hi)

    def _site_depth_override(self, profile_id: str) -> tuple:
        minutes = self._session_minutes(profile_id)
        frac = (minutes - 15) / 105.0
        mid = 3 + frac * 12
        lo = max(3, int(round(mid - 3)))
        hi = min(15, int(round(mid + 3)))
        if lo > hi:
            lo, hi = 3, 15
        d = random.randint(lo, hi)
        # Cap depth if little time remains (~8s per page)
        left = self._time_left(profile_id)
        max_by_time = max(3, int(left / 8))
        d = min(d, max_by_time, 15)
        d = max(3, d)
        return (d, d)

    def _sites_budget(self, profile_id: str) -> int:
        persona = self._personas.get(profile_id, {})
        try:
            n = int(persona.get("sites_budget") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < 15:
            n = self._scale_sites(profile_id)
        return max(15, min(40, n))

    def _time_almost_gone(self, profile_id: str, cushion: float = 60.0) -> bool:
        if self._has_pending_target(profile_id):
            return self._time_left(profile_id) < self._target_reserve_s(profile_id)
        return self._time_left(profile_id) < cushion

    def _has_pending_target(self, profile_id: str) -> bool:
        return bool(
            self._pcfg(profile_id, "target_warmup_enabled", False)
            and (self._pcfg(profile_id, "target_website", "") or "").strip()
        )

    def _target_reserve_s(self, profile_id: str) -> float:
        """Seconds kept for the configured target before ambient phases stop."""
        minutes = self._session_minutes(profile_id)
        return max(120.0, min(minutes * 60 * 0.25, 5 * 60))

    def _normalize_target_url(self, target_url: str) -> str:
        url = (target_url or "").strip()
        if url and not url.startswith("http"):
            url = "https://" + url
        return url

    async def _last_chance_target_visit(self, human, page, context,
                                        profile_id: str, target_url: str,
                                        timeout_s: float = 180.0) -> bool:
        """Open the target directly when Google/timer left no room for organic entry."""
        url = self._normalize_target_url(target_url)
        if not url:
            return False
        try:
            host = urlparse(url).netloc.replace("www.", "") or url[:40]
        except Exception:
            host = url[:40]
        self._log(profile_id, f"Last-chance: opening {host} directly (timer reserved/expired)")
        self._activity(profile_id, f"Opening {host} directly — timer left no time for Google")
        try:
            await asyncio.wait_for(
                human._direct_warmup_visit(
                    page, url, context=context, depth_override=(2, 4),
                ),
                timeout=max(45.0, float(timeout_s)),
            )
            self._mark_visited(profile_id, url)
            return True
        except asyncio.TimeoutError:
            self._log(profile_id, f"Last-chance visit of {host} hit the time cap")
            return True
        except Exception as e:
            self._log(profile_id, f"Last-chance visit failed: {e}")
            return False

    def _profile_info(self, profile_id: str) -> dict:
        store = self.config.get("profile_info") or {}
        pinfo = store.get(profile_id) or store.get(str(profile_id)) or {}
        return pinfo if isinstance(pinfo, dict) else {}

    async def _refresh_profile_proxy(self, profile_id: str) -> dict:
        """Re-fetch user_proxy_config from AdsPower and cache it on profile_info."""
        pinfo = dict(self._profile_info(profile_id))
        user_id = str(pinfo.get("user_id") or "").strip()
        if not user_id:
            try:
                user_id = await self.browser_mgr.resolve_user_id(profile_id)
            except Exception as e:
                logger.warning(f"[{profile_id}] Could not resolve user_id for proxy: {e}")
                user_id = ""
        cfg = {}
        try:
            if user_id:
                cfg = await self.browser_mgr.get_profile_proxy(user_id=user_id)
            if not cfg:
                cfg = await self.browser_mgr.get_profile_proxy(serial=str(profile_id))
        except Exception as e:
            logger.warning(f"[{profile_id}] AdsPower proxy re-fetch failed: {e}")
            cfg = {}
        if cfg:
            pinfo["proxy"] = cfg
            if user_id:
                pinfo["user_id"] = user_id
            self.config.setdefault("profile_info", {})[str(profile_id)] = pinfo
        return cfg if isinstance(cfg, dict) else {}

    # ── Profile notes (remark) ────────────────────────────────────

    async def _write_warmup_note(self, profile_id: str, status: str,
                                  duration_m: int = 0, target: str = ""):
        """Append a concise warmup note to the profile's remark in AdsPower.

        Format per line:  DD.MM HH:MM | 12m | site.com | OK
        Keeps existing notes, prepends new line at the top.
        """
        pinfo = self._profile_info(profile_id)
        user_id = str(pinfo.get("user_id") or "").strip()
        if not user_id:
            try:
                user_id = await self.browser_mgr.resolve_user_id(profile_id)
            except Exception as e:
                self._log(profile_id, f"Could not resolve user_id: {e}")
                user_id = ""
            if user_id:
                pinfo["user_id"] = user_id
                self.config.setdefault("profile_info", {})[str(profile_id)] = pinfo
        if not user_id:
            self._log(profile_id, "No user_id — skipping remark update")
            return

        now = datetime.now()
        date_str = now.strftime("%d.%m %H:%M")

        site_short = ""
        if target:
            site_short = target.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
        if not site_short:
            site_short = "notarget"

        if status == "completed":
            tag = "OK"
        elif status == "stopped":
            tag = "CANCELLED"
        elif status == "failed":
            tag = "FAIL"
        else:
            tag = status.upper()

        if status == "completed":
            note_line = f"{date_str} | {duration_m}m | {site_short} | {tag}"
        elif status == "stopped":
            if duration_m:
                note_line = f"{date_str} | {duration_m}m | {site_short} | {tag}"
            else:
                note_line = f"{date_str} | {site_short} | {tag}"
        else:
            note_line = f"{date_str} | {site_short} | {tag}"

        try:
            old_remark = await self.browser_mgr.get_profile_remark(user_id)
            if old_remark and old_remark.strip():
                new_remark = note_line + "\n" + old_remark.strip()
            else:
                new_remark = note_line

            ok = await self.browser_mgr.update_profile_remark(user_id, new_remark)
            if ok:
                self._log(profile_id, f"Remark updated: {note_line}")
            else:
                self._log(profile_id, "Remark update failed")
        except Exception as e:
            self._log(profile_id, f"Remark update error: {e}")

    # ── Core profile warmup ───────────────────────────────────────

    async def _warmup_profile(self, profile_id: str, semaphore: asyncio.Semaphore):
        """Run the full warmup pipeline for a single profile."""
        # Initialize metrics BEFORE try/semaphore so exception handlers always have it
        metrics = ProfileMetrics(profile_id)
        self._metrics[profile_id] = metrics
        persona_data = self._personas.get(profile_id, {})
        metrics.persona = persona_data.get("persona_name", "Unknown")

        # Create per-profile human simulator with activity callback
        human = self._get_human(profile_id)
        self._activity(profile_id, "Waiting for slot...")

        async with semaphore:
            await self._warmup_profile_core(profile_id, metrics, human, persona_data)

    async def _warmup_profile_core(self, profile_id: str, metrics: ProfileMetrics,
                                    human: 'HumanSimulator', persona_data: dict):
        """Core warmup logic (shared between semaphore and thread-safe versions)."""
        self._update_status(profile_id, ProfileStatus.STARTING)
        self.progress_store.update_profile(profile_id, "starting")
        self._activity(profile_id, "Starting browser...")
        self._log(profile_id, "Starting browser...")

        browser = None

        try:
            ws_endpoint = await self._start_or_attach_browser(profile_id)
            self._log(profile_id, f"Connected: {ws_endpoint[:60]}...")

            async with async_playwright() as pw:
                browser = await self._connect_cdp(pw, ws_endpoint, profile_id)
                self._open_browsers[profile_id] = browser
                if not browser.contexts:
                    context = await browser.new_context()
                else:
                    context = browser.contexts[0]
                await Stealth().apply_stealth_async(context)
                page = await self._ensure_page(context, browser)
                human.setup_dialog_handler(page)
                await human.sync_viewport_to_window(page)
                await human.maybe_toggle_bookmarks_bar(page)

                self._set_deadline(profile_id)
                mins = self._session_minutes(profile_id)
                self._log(profile_id, f"Session timer: {mins}m — hard stop when it hits zero")

                # Apply bandwidth saver if enabled (context-level = all tabs)
                if self._pcfg(profile_id, "bandwidth_saver", False):
                    self._log(profile_id, "Bandwidth saver ON — blocking images & heavy media")
                    await human.enable_bandwidth_saver(context)

                # (#6) Execute the session shape phases
                shape = persona_data.get("session_shape") or SESSION_SHAPES[0]
                self._log(profile_id, f"Session shape: {shape.get('name', '?')} — {shape.get('description', '')}")

                phases = shape.get("phases", ["ramp", "recon", "idle"])
                total_phases = len(phases)
                target_url = self._pcfg(profile_id, "target_website", "")
                target_enabled = self._pcfg(profile_id, "target_warmup_enabled", False)

                try:
                    for phase_idx, phase_name in enumerate(phases):
                        self._check_deadline(profile_id)
                        await self._wait_if_paused(profile_id)

                        remaining_phases = [p for p in phases[phase_idx + 1:]]
                        next_up = remaining_phases[0] if remaining_phases else "targeted warmup" if self._pcfg(profile_id, "target_warmup_enabled", False) else "finish"

                        if phase_name in ("idle", "short_idle") and self._time_almost_gone(profile_id):
                            self._log(profile_id, f"  Skipping {phase_name} — under 60s left on the timer")
                            continue

                        try:
                            if phase_name == "ramp":
                                self._update_status(profile_id, ProfileStatus.PHASE1)
                                self.progress_store.update_profile(profile_id, "phase1")
                                total_sites = self._sites_budget(profile_id)
                                ramp_n = max(3, int(total_sites * 0.4))
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] Ramp — browsing ~{ramp_n} sites in tabs")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: Ramp — multi-tab browsing ~{ramp_n} sites | Next → {next_up}")
                                await self._phase_ramp(page, context, profile_id, metrics)

                            elif phase_name == "recon":
                                self._update_status(profile_id, ProfileStatus.PHASE2)
                                self.progress_store.update_profile(profile_id, "phase2")
                                total_sites = self._sites_budget(profile_id)
                                recon_n = max(5, total_sites - int(total_sites * 0.4))
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] Recon — deep-exploring ~{recon_n} sites")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: Recon — deep exploration of ~{recon_n} sites | Next → {next_up}")
                                await self._phase_recon(page, context, profile_id, metrics)

                            elif phase_name == "idle":
                                self._update_status(profile_id, ProfileStatus.PHASE3)
                                self.progress_store.update_profile(profile_id, "phase3")
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] Idle — background browsing & waiting")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: Long idle — background activity | Next → {next_up}")
                                await self._phase_idle(context, profile_id, metrics, duration="long")

                            elif phase_name == "short_idle":
                                self._update_status(profile_id, ProfileStatus.PHASE3)
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] Short break — quick pause between phases")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: Short idle | Next → {next_up}")
                                await self._phase_idle(context, profile_id, metrics, duration="short")

                            elif phase_name == "youtube":
                                if not self._pcfg(profile_id, "youtube_enabled", True):
                                    self._log(profile_id, f"  Phase {phase_idx+1}/{total_phases}: YouTube — skipped (disabled)")
                                    continue
                                if self._time_almost_gone(profile_id):
                                    self._log(profile_id, "  Skipping YouTube — under 60s left on the timer")
                                    continue
                                self._update_status(profile_id, ProfileStatus.PHASE2)
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] YouTube — searching & watching videos")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: YouTube watching | Next → {next_up}")
                                await self._phase_youtube(page, context, profile_id, metrics)

                            elif phase_name == "search_burst":
                                self._update_status(profile_id, ProfileStatus.PHASE1)
                                self._activity(profile_id, f"[{phase_idx+1}/{total_phases}] Search burst — rapid Google searches")
                                self._log(profile_id, f"Phase {phase_idx+1}/{total_phases}: Search burst | Next → {next_up}")
                                await self._phase_search_burst(page, context, profile_id, metrics)

                        except _SkipPhase:
                            self._log(profile_id, f"  Skipped phase: {phase_name}")
                            self._activity(profile_id, f"Skipped {phase_name} → next")
                            continue

                        # (#2) Natural tab cleanup between phases
                        alive = [p for p in context.pages if not p.is_closed()]
                        if len(alive) > 3 and random.random() < 0.5:
                            closed = await human.close_random_tab(context, keep_minimum=2)
                            if closed:
                                self._log(profile_id, "  Closed a finished tab")

                    # ── Step 2: Targeted Website Warmup ──────────
                    await self._wait_if_paused(profile_id)
                    if target_enabled and target_url and self._time_almost_gone(profile_id):
                        self._log(
                            profile_id,
                            "Ambient phases stopped — reserving remaining time for the target site",
                        )
                except _TimeLimit:
                    self._log(
                        profile_id,
                        "Time limit reached during ambient phases — visiting target before wrap-up",
                    )
                    self._activity(profile_id, "Timer up — opening target site")

                if target_enabled and target_url:
                    try:
                        self._update_status(profile_id, ProfileStatus.TARGETED)
                        self.progress_store.update_profile(profile_id, "targeted")

                        target_url = self._normalize_target_url(target_url)
                        human.set_target_host(target_url)

                        target_page = await self._ensure_page(context, browser)
                        try:
                            if not target_page.is_closed():
                                human.setup_dialog_handler(target_page)
                        except Exception:
                            target_page = await self._new_page(context, browser)
                            human.setup_dialog_handler(target_page)

                        target_domain = target_url.replace("https://", "").replace(
                            "http://", "").split("/")[0].replace("www.", "")
                        site_name = target_domain.split(".")[0]
                        left = self._time_left(profile_id)
                        use_direct = left < 90 or getattr(human, "_google_blocked", False)

                        if use_direct:
                            await self._last_chance_target_visit(
                                human, target_page, context, profile_id, target_url,
                                timeout_s=max(90.0, left if left > 0 else 120.0),
                            )
                            metrics.record_page_visit(target_url)
                        else:
                            self._activity(
                                profile_id,
                                f"Step 2: Targeted warmup of {target_url[:40]} — searching Google...",
                            )
                            self._log(
                                profile_id,
                                f"Step 2: Targeted warmup → {target_url} "
                                f"(organic Google entry → deep exploration)",
                            )

                            target_queries = [
                                human._url_to_search_query(target_url),
                                f"{site_name} review",
                                f"{site_name} skins",
                                f"buy items {site_name}",
                                f"{site_name} marketplace",
                            ]
                            persona_queries = list(persona_data.get("queries", []))
                            if persona_queries:
                                for q in random.sample(persona_queries, min(4, len(persona_queries))):
                                    word = q.split()[0]
                                    if word.isalpha() and len(word) > 2:
                                        target_queries.append(q if len(q) < 50 else word)
                            target_queries = [q for q in target_queries if q and "/" not in q]

                            t_depth = self._site_depth_override(profile_id)
                            self._log(
                                profile_id,
                                f"  Plan: 1 Google landing, {t_depth[0]}-{t_depth[1]} pages deep",
                            )
                            try:
                                await asyncio.wait_for(
                                    human.targeted_site_warmup(
                                        target_page, context, target_url,
                                        search_queries=target_queries,
                                        num_visits=1,
                                        depth_per_visit=t_depth,
                                        custom_text=persona_data.get("custom_text", ""),
                                        metrics=metrics,
                                    ),
                                    timeout=max(30.0, left),
                                )
                                self._mark_visited(profile_id, target_url)
                                self._log(
                                    profile_id,
                                    f"Step 2: Targeted warmup complete — {site_name} explored deeply",
                                )
                                self._activity(
                                    profile_id,
                                    f"Step 2 done ({site_name}) — collecting cookies...",
                                )
                                metrics.record_page_visit(target_url)
                            except asyncio.TimeoutError:
                                self._log(
                                    profile_id,
                                    "Targeted Google path hit the timer — opening site directly",
                                )
                                await self._last_chance_target_visit(
                                    human, target_page, context, profile_id, target_url,
                                    timeout_s=120.0,
                                )
                                metrics.record_page_visit(target_url)
                    except _SkipPhase:
                        self._log(profile_id, "Step 2: Skipped targeted warmup")
                        self._activity(profile_id, "Skipped Step 2 → finishing")

                # Collect and analyze cookies for scoring
                self._activity(profile_id, "Collecting cookies & session data...")
                try:
                    alive = [p for p in context.pages if not p.is_closed()]
                    if alive:
                        cookies = await context.cookies()
                        score_host = ""
                        if target_url:
                            try:
                                score_host = urlparse(target_url).netloc.replace(
                                    "www.", ""
                                )
                            except Exception:
                                score_host = ""
                        metrics.analyze_cookies(
                            cookies, metrics.unique_domains,
                            target_domain=score_host,
                        )
                        self._log(profile_id, metrics.cookie_log_line())
                except Exception:
                    pass

                metrics.finish()
                elapsed_m = int(metrics.duration_s / 60) if metrics.duration_s else 0
                pages = len(metrics.visited_urls) if hasattr(metrics, 'visited_urls') else 0
                cookie_count = getattr(metrics, 'cookies_count', 0)
                self._update_status(profile_id, ProfileStatus.COMPLETED)
                self._activity(profile_id,
                               f"Done! {elapsed_m}m | {pages} pages | {cookie_count} cookies")
                self.progress_store.update_profile(profile_id, "completed")
                self._log(profile_id,
                          f"Warmup complete! Duration: {elapsed_m}m | "
                          f"Pages visited: {pages} | Cookies: {cookie_count}")

                self.session_memory.update_profile(
                    profile_id,
                    persona=metrics.persona,
                    visited_sites=metrics.visited_urls,
                    queries_used=metrics.queries_used,
                )

                await self._write_warmup_note(
                    profile_id, "completed", elapsed_m, target_url)

        except _StopRequested:
            metrics.finish()
            stopped_m = int(metrics.duration_s / 60) if metrics.duration_s else 0
            self._update_status(profile_id, ProfileStatus.STOPPED)
            self._activity(profile_id, "Stopped by user")
            self.progress_store.update_profile(profile_id, "stopped")
            self._log(profile_id, "Stopped by user")
            try:
                t_url = self._pcfg(profile_id, "target_website", "")
                await self._write_warmup_note(
                    profile_id, "stopped", stopped_m, t_url)
            except Exception:
                pass
            raise
        except BrowserManagerError as e:
            metrics.record_error(str(e))
            metrics.finish()
            self._update_status(profile_id, ProfileStatus.FAILED)
            self._error(profile_id, str(e))
            self._activity(profile_id, f"FAILED: {str(e)[:50]}")
            self.progress_store.update_profile(profile_id, "failed")
            self._log(profile_id, f"Browser error: {e}")
            try:
                t_url = self._pcfg(profile_id, "target_website", "")
                await self._write_warmup_note(profile_id, "failed", 0, t_url)
            except Exception:
                pass
            raise
        except Exception as e:
            metrics.record_error(str(e))
            metrics.finish()
            self._update_status(profile_id, ProfileStatus.FAILED)
            self._error(profile_id, str(e))
            self._activity(profile_id, f"FAILED: {str(e)[:50]}")
            self.progress_store.update_profile(profile_id, "failed")
            self._log(profile_id, f"Error: {e}")
            try:
                t_url = self._pcfg(profile_id, "target_website", "")
                await self._write_warmup_note(profile_id, "failed", 0, t_url)
            except Exception:
                pass
            raise
        finally:
            self._open_browsers.pop(profile_id, None)
            self._deadlines.pop(profile_id, None)
            await self._cleanup_browser(browser, profile_id)

    # ══════════════════════════════════════════════════════════════
    #  PHASE: RAMP (multi-tab browsing with tab lifecycle)
    # ══════════════════════════════════════════════════════════════

    async def _phase_ramp(self, page, context, profile_id: str, metrics: ProfileMetrics):
        human = self._get_human(profile_id)
        persona = self._personas.get(profile_id, {})
        sites = list(persona.get("sites", self.config.get("sites", [])))
        sites = self._strip_youtube(sites, profile_id)
        queries = list(persona.get("queries", self.config.get("search_queries", [])))
        random.shuffle(sites)

        # Each site draws its own scaled depth (3–15, based on session minutes)
        # Ramp gets ~40% of the session site budget
        total_sites_wanted = self._sites_budget(profile_id)
        ramp_count = max(3, int(total_sites_wanted * 0.4))

        tabs_min = int(self.config.get("timing", {}).get("tabs_min", 3) or 3)
        tabs_max = int(self.config.get("timing", {}).get("tabs_max", 5) or 5)
        if tabs_min > tabs_max:
            tabs_min, tabs_max = tabs_max, tabs_min
        tabs_min = max(1, tabs_min)
        tabs_max = max(tabs_min, tabs_max)
        num_tabs = random.randint(tabs_min, tabs_max)
        phase1_sites = sites[:ramp_count]

        for i, url in enumerate(phase1_sites):
            self._check_deadline(profile_id)
            await self._wait_if_paused(profile_id)
            if self._time_almost_gone(profile_id):
                if self._has_pending_target(profile_id):
                    self._log(profile_id, "  Ramp stopping — reserving time for the target site")
                    return
                self._log(profile_id, "  Ramp stopping — not enough time for another site")
                raise _TimeLimit()

            # Extract domain for display
            try:
                _domain = urlparse(url).netloc.replace("www.", "") or url[:30]
            except Exception:
                _domain = url[:30]
            sites_left = len(phase1_sites) - i - 1

            if ((self._already_visited(profile_id, url) or self._is_session_target(profile_id, url))
                    and "google.com" not in _domain):
                why = "target reserved for Google Site Warmup" if self._is_session_target(profile_id, url) \
                    else "already visited this session"
                self._log(profile_id, f"  Ramp skip {_domain} ({why})")
                continue

            self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] {_domain} | {sites_left} sites left")

            if i == 0:
                target = page
            else:
                target = await self._new_page(context)
                human.setup_dialog_handler(target)
                await human.sync_viewport_to_window(target)
                metrics.record_tab()

            try:
                site_depth = self._site_depth_override(profile_id)
                depth_info = f"deep {site_depth[0]}-{site_depth[1]} pages"

                # Navigation: Google click-through only. On miss, skip the site.
                if url.startswith("https://www.google.com") and queries:
                    if random.random() < 0.6:
                        query = random.choice(queries)
                        target_domains = [s for s in sites if not s.startswith("https://www.google")][:10]
                        self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] Googling: \"{query[:30]}\"")
                        self._log(profile_id, f"  [{i+1}/{len(phase1_sites)}] Google search: \"{query}\" ({depth_info})")
                        await human.simulate_google_search(target, query, target_domains=target_domains)
                        metrics.record_search(query)
                    else:
                        self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] Browsing Google services")
                        self._log(profile_id, f"  [{i+1}/{len(phase1_sites)}] Google browse (no search)")
                        ok = await human.safe_navigate(target, "https://www.google.com")
                        if ok:
                            await asyncio.sleep(random.uniform(3, 8))
                            await human.scroll_page(target)
                            await human.move_mouse_randomly(target)
                            await asyncio.sleep(random.uniform(2, 5))
                else:
                    self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] Searching Google for {_domain} ({depth_info})")
                    self._log(profile_id, f"  [{i+1}/{len(phase1_sites)}] Google → {_domain} ({depth_info})")
                    found = await human.search_and_visit_site(
                        target, url, context=context, depth_override=site_depth
                    )
                    metrics.record_search(human._url_to_search_query(url))
                    if not found:
                        self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] Search miss — skipping {_domain}")
                        self._log(profile_id, f"    Not found in Google — skip {_domain}")
                        continue
                    self._mark_visited(profile_id, url)
                    metrics.record_page_visit(url)
                    metrics.record_link_click()

                if target.is_closed():
                    self._log(profile_id, f"    Tab closed during {_domain} — moving on")
                    continue

                if random.random() < 0.25:
                    self._activity(profile_id, f"Ramp [{i+1}/{len(phase1_sites)}] Filling out forms on {_domain}")
                    await human.interact_with_page_forms(target)

                await human.simulate_reading(target)
                metrics.record_scroll()

            except (_StopRequested, _SkipPhase, _TimeLimit):
                raise
            except Exception as e:
                # Skip this site on error and move to the next one
                self._error(profile_id, f"Skipped {url[:40]}: {e}")
                self._log(profile_id, f"  ERROR on {url[:40]}: {e} — skipping")
                metrics.record_error(str(e))
                continue

            # (#2) Close a tab if we have too many open
            alive = [p for p in context.pages if not p.is_closed()]
            if len(alive) > num_tabs and random.random() < 0.4:
                await human.close_random_tab(context, keep_minimum=2)
                self._log(profile_id, "  Closed a tab (natural lifecycle)")

            if len(alive) > 1 and random.random() < 0.4:
                switched = await human.switch_to_random_tab(context)
                if switched and not switched.is_closed():
                    await human.simulate_reading(switched)
                    metrics.record_scroll()

        self._log(profile_id, f"  Ramp phase complete — visited {len(phase1_sites)} sites")
        self._activity(profile_id, f"Ramp done ({len(phase1_sites)} sites visited) — moving to next phase")

    # ══════════════════════════════════════════════════════════════
    #  PHASE: RECON (deep exploration — #10 more sites)
    # ══════════════════════════════════════════════════════════════

    async def _phase_recon(self, page, context, profile_id: str, metrics: ProfileMetrics):
        human = self._get_human(profile_id)
        persona = self._personas.get(profile_id, {})
        sites = list(persona.get("sites", self.config.get("sites", [])))
        sites = self._strip_youtube(sites, profile_id)
        queries = list(persona.get("queries", self.config.get("search_queries", [])))

        # Recon gets the remaining session site budget
        total_sites_wanted = self._sites_budget(profile_id)
        recon_count = max(5, total_sites_wanted - int(total_sites_wanted * 0.4))

        sites = [s for s in sites
                 if not self._already_visited(profile_id, s)
                 and not self._is_session_target(profile_id, s)]
        random.shuffle(sites)
        recon_sites = list(sites)

        if not recon_sites:
            self._log(profile_id, "  No unvisited sites available for recon — skipping")
            return

        num_recon = min(recon_count, len(recon_sites))
        recon_sites = recon_sites[:num_recon]

        for idx, url in enumerate(recon_sites):
            self._check_deadline(profile_id)
            await self._wait_if_paused(profile_id)
            if self._time_almost_gone(profile_id):
                if self._has_pending_target(profile_id):
                    self._log(profile_id, "  Recon stopping — reserving time for the target site")
                    return
                self._log(profile_id, "  Recon stopping — not enough time for another site")
                raise _TimeLimit()

            # Extract domain for display
            try:
                _domain = urlparse(url).netloc.replace("www.", "") or url[:30]
            except Exception:
                _domain = url[:30]
            sites_left = num_recon - idx - 1

            if ((self._already_visited(profile_id, url) or self._is_session_target(profile_id, url))
                    and "google.com" not in _domain):
                why = "target reserved for Google Site Warmup" if self._is_session_target(profile_id, url) \
                    else "already visited this session"
                self._log(profile_id, f"  Recon skip {_domain} ({why})")
                continue

            # Scaled depth for this site (3–15)
            site_depth = self._site_depth_override(profile_id)
            depth_info = f"deep {site_depth[0]}-{site_depth[1]} pages"

            self._activity(profile_id, f"Recon [{idx+1}/{num_recon}] {_domain} ({depth_info}) | {sites_left} left")

            alive_pages = [p for p in context.pages if not p.is_closed()]
            if alive_pages:
                target = random.choice(alive_pages)
            else:
                target = await self._new_page(context)
                human.setup_dialog_handler(target)

            try:
                # Navigation: Google click-through only. On miss, skip the site.
                if url.startswith("https://www.google.com") and queries:
                    if random.random() < 0.6:
                        query = random.choice(queries)
                        target_domains = [s for s in sites if not s.startswith("https://www.google")][:10]
                        self._activity(profile_id, f"Recon [{idx+1}/{num_recon}] Googling: \"{query[:30]}\"")
                        self._log(profile_id, f"  [{idx+1}/{num_recon}] Google search: \"{query}\" ({depth_info})")
                        await human.simulate_google_search(target, query, target_domains=target_domains)
                        metrics.record_search(query)
                    else:
                        self._activity(profile_id, f"Recon [{idx+1}/{num_recon}] Browsing Google services")
                        self._log(profile_id, f"  [{idx+1}/{num_recon}] Google browse (no search)")
                        ok = await human.safe_navigate(target, "https://www.google.com")
                        if ok:
                            await asyncio.sleep(random.uniform(3, 8))
                            await human.scroll_page(target)
                            await human.move_mouse_randomly(target)
                            await asyncio.sleep(random.uniform(2, 5))
                else:
                    extra_q = None
                    if queries and random.random() < 0.25:
                        q = random.choice(queries)
                        try:
                            domain_base = urlparse(url).netloc.replace("www.", "").split(".")[0]
                            extra_q = f"{q} {domain_base}"
                        except Exception:
                            extra_q = None
                    self._activity(profile_id, f"Recon [{idx+1}/{num_recon}] Searching Google for {_domain}")
                    self._log(profile_id, f"  [{idx+1}/{num_recon}] Google → {_domain} ({depth_info})")
                    found = await human.search_and_visit_site(
                        target, url, extra_query=extra_q,
                        context=context, depth_override=site_depth
                    )
                    metrics.record_search(human._url_to_search_query(url))
                    if not found:
                        self._activity(profile_id, f"Recon [{idx+1}/{num_recon}] Search miss — skipping {_domain}")
                        self._log(profile_id, f"    Not found in Google — skip {_domain}")
                        continue
                    self._mark_visited(profile_id, url)
                    metrics.record_page_visit(url)
                    metrics.record_link_click()

                if target.is_closed():
                    self._log(profile_id, f"    Tab closed during {_domain} — moving on")
                    continue

                if random.random() < 0.25:
                    await human.interact_with_page_forms(target)

            except (_StopRequested, _SkipPhase, _TimeLimit):
                raise
            except Exception as e:
                self._error(profile_id, f"Skipped {url[:40]}: {e}")
                self._log(profile_id, f"  ERROR on {url[:40]}: {e} — skipping")
                metrics.record_error(str(e))
                continue

            alive = [p for p in context.pages if not p.is_closed()]
            if len(alive) > 4 and random.random() < 0.35:
                await human.close_random_tab(context, keep_minimum=2)

            if len(alive) > 1 and random.random() < 0.5:
                await human.switch_to_random_tab(context)

        self._log(profile_id, f"  Recon phase complete — deep-explored {num_recon} sites")
        self._activity(profile_id, f"Recon done ({num_recon} sites explored) — moving to next phase")

    # ══════════════════════════════════════════════════════════════
    #  PHASE: YOUTUBE (#4)
    # ══════════════════════════════════════════════════════════════

    async def _phase_youtube(self, page, context, profile_id: str, metrics: ProfileMetrics):
        """Watch 1-3 YouTube videos based on persona interests."""
        human = self._get_human(profile_id)
        persona = self._personas.get(profile_id, {})
        queries = list(persona.get("queries", self.config.get("search_queries", [])))

        if not queries:
            return

        num_videos = random.randint(1, 3)

        for v in range(num_videos):
            self._check_deadline(profile_id)
            await self._wait_if_paused(profile_id)
            if self._time_almost_gone(profile_id):
                if self._has_pending_target(profile_id):
                    self._log(profile_id, "  YouTube stopping — reserving time for the target site")
                    return
                self._log(profile_id, "  YouTube stopping — not enough time left")
                raise _TimeLimit()

            alive = [p for p in context.pages if not p.is_closed()]
            if alive:
                target = random.choice(alive)
            else:
                target = await self._new_page(context)
                human.setup_dialog_handler(target)

            query = random.choice(queries)
            yt_query = " ".join(query.split()[:5])

            self._activity(profile_id, f"YouTube [{v+1}/{num_videos}] Searching: \"{yt_query[:35]}\"")
            self._log(profile_id, f"  YouTube [{v+1}/{num_videos}]: \"{yt_query[:40]}\"")
            try:
                await human.watch_youtube(target, yt_query)
                metrics.record_page_visit("https://www.youtube.com")
                metrics.record_scroll()
            except (_StopRequested, _SkipPhase, _TimeLimit):
                raise
            except Exception as e:
                self._error(profile_id, f"YouTube error: {e}")
                self._log(profile_id, f"    YouTube error: {e} — skipping")
                metrics.record_error(str(e))

            await self._cancellable_sleep(random.uniform(3, 8), profile_id=profile_id)

        self._log(profile_id, f"  YouTube phase complete — watched {num_videos} videos")
        self._activity(profile_id, f"YouTube done ({num_videos} videos) — moving on")

    # ══════════════════════════════════════════════════════════════
    #  PHASE: SEARCH BURST (#6 variation)
    # ══════════════════════════════════════════════════════════════

    async def _phase_search_burst(self, page, context, profile_id: str, metrics: ProfileMetrics):
        """Do 3-6 rapid Google searches with targeted click-throughs."""
        human = self._get_human(profile_id)
        persona = self._personas.get(profile_id, {})
        sites = list(persona.get("sites", self.config.get("sites", [])))
        sites = self._strip_youtube(sites, profile_id)
        queries = list(persona.get("queries", self.config.get("search_queries", [])))

        if not queries:
            return

        random.shuffle(queries)
        num_searches = random.randint(3, 6)
        target_domains = [s for s in sites if not s.startswith("https://www.google")][:15]

        for query in queries[:num_searches]:
            self._check_deadline(profile_id)
            await self._wait_if_paused(profile_id)
            if self._time_almost_gone(profile_id):
                if self._has_pending_target(profile_id):
                    self._log(profile_id, "  Search burst stopping — reserving time for the target site")
                    return
                self._log(profile_id, "  Search burst stopping — not enough time left")
                raise _TimeLimit()

            alive = [p for p in context.pages if not p.is_closed()]
            if alive:
                target = random.choice(alive)
            else:
                target = await self._new_page(context)
                human.setup_dialog_handler(target)

            search_idx = queries[:num_searches].index(query) + 1
            self._activity(profile_id, f"Search burst [{search_idx}/{num_searches}] \"{query[:35]}\"")
            self._log(profile_id, f"  Search [{search_idx}/{num_searches}]: \"{query[:50]}\"")
            try:
                await human.simulate_google_search(target, query, target_domains=target_domains)
                metrics.record_search(query)

                if not target.is_closed():
                    await human.simulate_reading(target)
                    metrics.record_scroll()
            except (_StopRequested, _SkipPhase, _TimeLimit):
                raise
            except Exception as e:
                self._error(profile_id, f"Search error: {e}")
                self._log(profile_id, f"    Search error: {e} — skipping")
                metrics.record_error(str(e))

            await self._cancellable_sleep(random.uniform(2, 6), profile_id=profile_id)

        self._log(profile_id, f"  Search burst complete — {num_searches} searches")
        self._activity(profile_id, f"Search burst done ({num_searches} searches) — moving on")

    # ══════════════════════════════════════════════════════════════
    #  PHASE: IDLE (#3 — active idle with new browsing)
    # ══════════════════════════════════════════════════════════════

    async def _phase_idle(self, context, profile_id: str, metrics: ProfileMetrics,
                          duration: str = "long"):
        """
        Idle phase with occasional active browsing.

        (#3) Unlike the old version, this idle phase doesn't just wiggle the mouse.
        It occasionally:
        - Visits a new site from the persona pool
        - Does a quick Google search
        - Watches a short YouTube clip
        - Interacts with an already-open page
        """
        human = self._get_human(profile_id)
        persona = self._personas.get(profile_id, {})
        sites = list(persona.get("sites", self.config.get("sites", [])))
        sites = self._strip_youtube(sites, profile_id)
        queries = list(persona.get("queries", self.config.get("search_queries", [])))

        if duration == "short":
            idle_min = self.config.get("timing", {}).get("idle_min_minutes", 30) * 0.3
            idle_max = self.config.get("timing", {}).get("idle_max_minutes", 60) * 0.3
        else:
            idle_min = self.config.get("timing", {}).get("idle_min_minutes", 30)
            idle_max = self.config.get("timing", {}).get("idle_max_minutes", 60)

        total_idle = random.uniform(idle_min * 60, idle_max * 60)
        remaining = self._time_left(profile_id)
        if self._has_pending_target(profile_id):
            remaining = max(0.0, remaining - self._target_reserve_s(profile_id))
        if remaining < 60:
            self._log(profile_id, "  Skipping idle — reserving remaining time for the target site"
                      if self._has_pending_target(profile_id)
                      else "  Skipping idle — under 60s left on the timer")
            return
        total_idle = min(total_idle, remaining)
        idle_mins = int(total_idle / 60)
        self._log(profile_id, f"  Idling for ~{idle_mins} minutes (background browsing, scrolling, searching)")
        self._activity(profile_id, f"Idle phase — ~{idle_mins}m of background activity")

        elapsed = 0.0
        sites_visited_in_idle = 0

        while elapsed < total_idle:
            if self._time_almost_gone(profile_id) and self._has_pending_target(profile_id):
                self._log(profile_id, "  Idle stopping — reserving time for the target site")
                return
            self._check_deadline(profile_id)

            gap = random.choice([
                random.uniform(15, 40),
                random.uniform(40, 80),
                random.uniform(80, 150),
            ])
            usable = self._time_left(profile_id)
            if self._has_pending_target(profile_id):
                usable = max(0.0, usable - self._target_reserve_s(profile_id))
            sleep_time = min(gap, total_idle - elapsed, max(0.0, usable))
            if sleep_time <= 0:
                if self._has_pending_target(profile_id):
                    self._log(profile_id, "  Idle stopping — reserving time for the target site")
                    return
                raise _TimeLimit()
            await self._cancellable_sleep(sleep_time, profile_id=profile_id)
            elapsed += sleep_time

            if elapsed >= total_idle:
                break

            self._check_deadline(profile_id)
            await self._wait_if_paused(profile_id)

            try:
                alive_pages = [p for p in context.pages if not p.is_closed()]
            except Exception:
                alive_pages = []

            if not alive_pages:
                try:
                    new_p = await self._new_page(context)
                    human.setup_dialog_handler(new_p)
                    alive_pages = [new_p]
                except Exception:
                    self._log(profile_id, "  Cannot create page in idle — ending phase")
                    break

            target = random.choice(alive_pages)

            # (#3) Active idle activities with higher weights for actual browsing
            activity = random.choices(
                ["nothing", "scroll", "mouse", "tab_switch",
                 "scroll_and_mouse", "hover", "select_text",
                 "visit_new_site", "quick_search", "youtube_clip",
                 "close_tab", "form_interact"],
                weights=[15, 15, 10, 10, 8, 4, 3,
                         12, 10, 5, 4, 4],
                k=1,
            )[0]

            mins_elapsed = int(elapsed / 60)
            mins_remaining = max(0, int((total_idle - elapsed) / 60))

            try:
                if activity == "nothing":
                    self._activity(profile_id, f"Idle — resting ({mins_remaining}m left)")

                elif activity == "scroll":
                    self._activity(profile_id, f"Idle — scrolling page ({mins_remaining}m left)")
                    await human.scroll_page(target)
                    metrics.record_scroll()

                elif activity == "mouse":
                    self._activity(profile_id, f"Idle — moving mouse ({mins_remaining}m left)")
                    await human.move_mouse_randomly(target)

                elif activity == "tab_switch" and len(alive_pages) > 1:
                    self._activity(profile_id, f"Idle — switching tab ({mins_remaining}m left)")
                    switched = await human.switch_to_random_tab(context)
                    if switched and not switched.is_closed():
                        if random.random() < 0.4:
                            await human.scroll_page(switched)
                            metrics.record_scroll()

                elif activity == "scroll_and_mouse":
                    self._activity(profile_id, f"Idle — scrolling & reading ({mins_remaining}m left)")
                    await human.scroll_page(target)
                    await human.move_mouse_randomly(target)
                    metrics.record_scroll()

                elif activity == "hover":
                    await human.hover_random_element(target)

                elif activity == "select_text":
                    await human.select_random_text(target)

                elif activity == "visit_new_site" and sites and sites_visited_in_idle < 5:
                    if self._time_almost_gone(profile_id):
                        continue
                    unvisited = [s for s in sites if not self._already_visited(profile_id, s)
                                 and not self._is_session_target(profile_id, s)
                                 and "google.com" not in (self._host_of(s) or "")]
                    if not unvisited:
                        continue
                    site = random.choice(unvisited)
                    try:
                        _sdomain = urlparse(site).netloc.replace("www.", "") or site[:30]
                    except Exception:
                        _sdomain = site[:30]
                    self._activity(profile_id, f"Idle — Google → {_sdomain} ({mins_remaining}m left)")
                    self._log(profile_id, f"  Idle Google: {_sdomain}")
                    found = await human.search_and_visit_site(
                        target, site, context=context,
                        depth_override=self._site_depth_override(profile_id)
                    )
                    if not found:
                        self._log(profile_id, f"    Idle skip {_sdomain} — not in Google")
                        continue
                    self._mark_visited(profile_id, site)
                    metrics.record_page_visit(site)
                    sites_visited_in_idle += 1

                elif activity == "quick_search" and queries:
                    query = random.choice(queries)
                    self._activity(profile_id, f"Idle — Googling \"{query[:30]}\" ({mins_remaining}m left)")
                    self._log(profile_id, f"  Idle search: \"{query[:40]}\"")
                    target_domains = [s for s in sites if not s.startswith("https://www.google")][:8]
                    await human.simulate_google_search(target, query, target_domains=target_domains)
                    metrics.record_search(query)

                elif activity == "youtube_clip" and queries:
                    if not self._pcfg(profile_id, "youtube_enabled", True):
                        continue
                    q = " ".join(random.choice(queries).split()[:4])
                    self._activity(profile_id, f"Idle — YouTube: \"{q[:25]}\" ({mins_remaining}m left)")
                    self._log(profile_id, f"  Idle YouTube: \"{q[:30]}\"")
                    await human.watch_youtube(target, q)
                    metrics.record_page_visit("https://www.youtube.com")

                elif activity == "close_tab":
                    closed = await human.close_random_tab(context, keep_minimum=1)
                    if closed:
                        self._log(profile_id, "  Closed a tab during idle")

                elif activity == "form_interact":
                    self._activity(profile_id, f"Idle — interacting with forms ({mins_remaining}m left)")
                    await human.interact_with_page_forms(target)

            except (_StopRequested, _SkipPhase, _TimeLimit):
                raise
            except Exception as e:
                self._error(profile_id, f"Idle error: {e}")
                pass

            mins_left = int((total_idle - elapsed) / 60)
            if mins_left > 0 and mins_left % 5 == 0:
                self._log(profile_id, f"  Idle: ~{mins_left}m remaining | {sites_visited_in_idle} sites browsed so far")

        self._log(profile_id, f"  Idle phase complete — {int(elapsed/60)}m elapsed, {sites_visited_in_idle} sites browsed")
        self._activity(profile_id, f"Idle done ({int(elapsed/60)}m) — moving to next phase")

    # ── Utilities ─────────────────────────────────────────────────

    async def _new_page(self, context, browser=None):
        """Open a page; map Playwright 'already closed' errors to a retryable failure."""
        try:
            if browser is not None and hasattr(browser, "is_connected") and not browser.is_connected():
                raise BrowserManagerError("Browser disconnected")
            return await context.new_page()
        except BrowserManagerError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(s in msg for s in ("closed", "target page", "has been closed", "disconnected")):
                raise BrowserManagerError(f"Browser context closed: {e}") from e
            raise

    async def _ensure_page(self, context, browser=None):
        """Reuse a live page, or open a new one if the context is still alive."""
        try:
            if browser is not None and hasattr(browser, "is_connected") and not browser.is_connected():
                raise BrowserManagerError("Browser disconnected")
            for p in list(context.pages or []):
                try:
                    if not p.is_closed():
                        return p
                except Exception:
                    continue
            return await self._new_page(context, browser)
        except BrowserManagerError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(s in msg for s in ("closed", "target page", "has been closed", "disconnected")):
                raise BrowserManagerError(f"Browser context closed: {e}") from e
            raise

    async def _cleanup_browser(self, browser, profile_id: str):
        """Safely close browser and stop the AdsPower profile."""
        try:
            if browser:
                await browser.close()
        except Exception as e:
            logger.debug(f"Browser close error for {profile_id}: {e}")
        try:
            await self.browser_mgr.stop_browser(profile_id)
        except Exception as e:
            logger.debug(f"Browser stop error for {profile_id}: {e}")
        self._log(profile_id, "Browser closed")

    def _check_stop(self, profile_id: str = None):
        if self._stop_event.is_set():
            raise _StopRequested()
        if profile_id and profile_id in self._stopped_profiles:
            raise _StopRequested()
        if profile_id and profile_id in self._deleted_profiles:
            self._log(profile_id, "Profile deleted in AdsPower — finishing current run gracefully")

    def mark_profile_deleted(self, profile_id: str):
        """Mark a profile as deleted so it won't be re-queued but current work finishes."""
        with self._state_lock:
            self._deleted_profiles.add(profile_id)

    MAX_PAUSE_SECONDS = 86400  # Safety: auto-resume after 24 hours

    async def _wait_if_paused(self, profile_id: str):
        """If the profile is paused, block here until resumed (or stopped).
        Safety timeout prevents indefinite blocking (24h max)."""
        if not self._paused.get(profile_id):
            return
        self._activity(profile_id, "Paused — work in browser, then Resume")
        waited = 0.0
        while self._paused.get(profile_id):
            if self._stop_event.is_set():
                raise _StopRequested()
            if profile_id in self._stopped_profiles:
                raise _StopRequested()
            if waited >= self.MAX_PAUSE_SECONDS:
                self._log(profile_id, "Pause timeout reached (24h) — auto-resuming")
                self._paused[profile_id] = False
                break
            await asyncio.sleep(1.0)
            waited += 1.0

    async def _cancellable_sleep(self, seconds: float, profile_id: str = None):
        elapsed = 0.0
        interval = 0.5  # Check stop every 0.5s for fast hard stop
        while elapsed < seconds:
            if self._stop_event.is_set():
                raise _StopRequested()
            # Check per-profile stop/skip during long sleeps
            if profile_id:
                if profile_id in self._stopped_profiles:
                    raise _StopRequested()
                skip_ev = self._skip_events.get(profile_id)
                if skip_ev and skip_ev.is_set():
                    skip_ev.clear()
                    raise _SkipPhase()
                if self._deadlines.get(profile_id) is not None and self._time_left(profile_id) <= 0:
                    raise _TimeLimit()
                # If paused, wait here until resumed (timer doesn't advance)
                if self._paused.get(profile_id):
                    await self._wait_if_paused(profile_id)
                    continue  # Re-check without advancing elapsed
            chunk = min(interval, seconds - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk

    def _update_status(self, profile_id: str, status):
        try:
            status_str = status.value if isinstance(status, ProfileStatus) else str(status)
            # Track last non-paused active status for resume restoration
            if status_str not in ("paused", "completed", "failed", "stopped"):
                self._paused_prev_status[profile_id] = status_str
            m = self._metrics.get(profile_id)
            if m and status_str in ("completed", "failed", "stopped"):
                m.status = status_str
            self._on_status(profile_id, status_str)
        except Exception:
            pass

    def _log(self, profile_id: str, message: str):
        try:
            self._on_log(profile_id, message)
        except Exception:
            pass

    def _activity(self, profile_id: str, text: str, also_log: bool = True):
        """Report live activity for a profile to the UI and log."""
        if self._on_activity:
            try:
                self._on_activity(profile_id, text)
            except Exception:
                pass
        # Always log activity for visibility in Activity Log panel
        if also_log:
            self._log(profile_id, text)

    def _error(self, profile_id: str, text: str):
        """Report a non-fatal error for a profile to the UI."""
        if self._on_error:
            try:
                self._on_error(profile_id, text)
            except Exception:
                pass

    def _get_human(self, profile_id: str) -> HumanSimulator:
        """Get or create a per-profile HumanSimulator with activity + skip + pause support."""
        if profile_id not in self._humans:
            # Create a skip event for this profile
            if profile_id not in self._skip_events:
                self._skip_events[profile_id] = threading.Event()

            notify_enabled = self.config.get("windows_notifications_enabled", True)

            def _captcha_cb(event_type: str, details: dict):
                """Cloudflare / leftover CAPTCHA notifications. Google Sorry is auto-solved."""
                if event_type == "cloudflare":
                    if notify_enabled:
                        notifications.notify_cloudflare_captcha(
                            profile_id, details.get("url", ""))
                    if self._on_notify:
                        try:
                            self._on_notify(profile_id, "cloudflare_captcha",
                                            details.get("url", ""))
                        except Exception:
                            pass

            _NET_COOLDOWN_S = 60  # one toast per profile per 60 s

            def _network_error_cb(url: str, error_str: str):
                self._record_net_strike(profile_id)
                now = time.time()
                last = self._net_error_cooldowns.get(profile_id, 0)
                if now - last < _NET_COOLDOWN_S:
                    return
                self._net_error_cooldowns[profile_id] = now
                if notify_enabled:
                    notifications.notify_no_internet(profile_id, url=url, error_detail=error_str)
                if self._on_notify:
                    try:
                        self._on_notify(profile_id, "internet_error",
                                        f"{url}|{error_str}")
                    except Exception:
                        pass

            p = self._personas.get(profile_id, {})
            try:
                google_budget = int(p.get("google_budget") or 0)
            except (TypeError, ValueError):
                google_budget = 0
            if google_budget < 1:
                google_budget = max(18, int(p.get("sites_budget") or self._scale_sites(profile_id)))

            self._humans[profile_id] = HumanSimulator(
                self._human_base_timing,
                activity_cb=lambda text: self._activity(profile_id, text),
                skip_event=self._skip_events[profile_id],
                pause_check=lambda: self._wait_if_paused(profile_id),
                bandwidth_saver=self._pcfg(profile_id, "bandwidth_saver", False),
                captcha_solver=self._captcha_solver,
                stop_check=lambda: self._stop_event.is_set() or profile_id in self._stopped_profiles,
                manual_captcha_cb=_captcha_cb,
                network_error_cb=_network_error_cb,
                nav_success_cb=lambda: self._clear_net_strikes(profile_id),
                search_gate=self._search_gate,
                google_blocked_initial=self.session_memory.google_recently_blocked(profile_id),
                on_google_blocked=lambda: self.session_memory.mark_google_blocked(profile_id),
                youtube_enabled=self._pcfg(profile_id, "youtube_enabled", True),
                interest_keywords=p.get("interest_keywords"),
                link_bias=p.get("link_bias"),
                google_budget=google_budget,
                proxy_config=self._profile_info(profile_id).get("proxy") or {},
                profile_id=str(profile_id),
                refresh_proxy=lambda pid=str(profile_id): self._refresh_profile_proxy(pid),
            )
            self._humans[profile_id].apply_persona_style(
                interest_keywords=p.get("interest_keywords"),
                link_bias=p.get("link_bias"),
                skip_auth_forms=p.get("skip_auth_forms"),
            )
            tw = self._pcfg(profile_id, "target_website", "")
            if tw:
                self._humans[profile_id].set_target_host(tw)
        return self._humans[profile_id]
