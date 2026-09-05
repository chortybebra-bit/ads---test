"""
Session Store — Handles session memory, progress save/resume, and health scoring.
Data is persisted as JSON files alongside the app.
"""

import json
import os
import time
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _data_dir():
    """Get the data directory (same folder as the app)."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = os.path.join(base, "data")
    try:
        os.makedirs(data, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create data directory {data}: {e}")
    return data


# ══════════════════════════════════════════════════════════════════
#  SESSION MEMORY — remembers what each profile did before
# ══════════════════════════════════════════════════════════════════

class SessionMemory:
    """
    Tracks what each profile has visited across multiple warmup runs.
    On subsequent runs, includes some previously visited sites for
    return-visitor patterns (much more realistic).
    """

    def __init__(self):
        self.path = os.path.join(_data_dir(), "session_memory.json")
        self.data: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load session memory: {e}")
                self.data = {}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except (OSError, TypeError) as e:
            logger.error(f"Failed to save session memory: {e}")

    def get_profile(self, profile_id: str) -> dict:
        """Get session data for a profile."""
        return self.data.get(profile_id, {
            "total_warmups": 0,
            "last_warmup": None,
            "persona": None,
            "visited_sites": [],
            "search_queries_used": [],
        })

    def update_profile(self, profile_id: str, persona: str,
                       visited_sites: list, queries_used: list):
        """Update session data after a warmup run."""
        existing = self.get_profile(profile_id)

        # Merge visited sites (keep unique in order, cap at 200)
        combined_sites = existing.get("visited_sites", []) + visited_sites
        seen_sites = set()
        all_sites = []
        for s in combined_sites:
            if s not in seen_sites:
                seen_sites.add(s)
                all_sites.append(s)
        if len(all_sites) > 200:
            all_sites = all_sites[-200:]

        combined_queries = existing.get("search_queries_used", []) + queries_used
        seen_queries = set()
        all_queries = []
        for q in combined_queries:
            if q not in seen_queries:
                seen_queries.add(q)
                all_queries.append(q)
        if len(all_queries) > 100:
            all_queries = all_queries[-100:]

        self.data[profile_id] = {
            "total_warmups": existing.get("total_warmups", 0) + 1,
            "last_warmup": datetime.now().isoformat(),
            "persona": persona,
            "visited_sites": all_sites,
            "search_queries_used": all_queries,
        }
        self._save()

    def get_return_sites(self, profile_id: str, count: int = 3) -> list:
        """
        Get previously visited sites to revisit (return visitor pattern).
        Returns up to `count` random sites from history.
        """
        existing = self.get_profile(profile_id)
        sites = existing.get("visited_sites", [])
        if not sites:
            return []
        import random
        return random.sample(sites, min(count, len(sites)))

    def mark_google_blocked(self, profile_id: str):
        """Remember that this profile just hit a Google CAPTCHA, so future
        runs/sessions avoid Google for this profile for a while."""
        entry = self.data.get(profile_id)
        if entry is None:
            entry = self.get_profile(profile_id)
            self.data[profile_id] = entry
        entry["google_blocked_at"] = time.time()
        self._save()

    def google_recently_blocked(self, profile_id: str, hours: float = 6.0) -> bool:
        """True if this profile hit a Google CAPTCHA within the last `hours`."""
        entry = self.data.get(profile_id, {})
        ts = entry.get("google_blocked_at", 0)
        if not ts:
            return False
        return (time.time() - ts) < hours * 3600


# ══════════════════════════════════════════════════════════════════
#  PROGRESS SAVE / RESUME — survive crashes
# ══════════════════════════════════════════════════════════════════

class ProgressStore:
    """
    Saves warmup progress so interrupted runs can be resumed.
    Tracks which profiles completed, failed, or are still pending.
    """

    def __init__(self):
        self.path = os.path.join(_data_dir(), "progress.json")
        self.data: Optional[dict] = None

    def has_incomplete_run(self) -> bool:
        """Check if there's a saved incomplete run."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Check if any profiles are not completed/failed
                profiles = data.get("profiles", {})
                return any(
                    p.get("status") not in ("completed", "failed", "stopped")
                    for p in profiles.values()
                )
            except (json.JSONDecodeError, OSError):
                return False
        return False

    def load_progress(self) -> Optional[dict]:
        """Load saved progress. Returns None if no saved progress."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                return self.data
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def get_remaining_profiles(self) -> list:
        """Get list of profile IDs that haven't completed yet."""
        if not self.data:
            self.load_progress()
        if not self.data:
            return []
        profiles = self.data.get("profiles", {})
        return [
            pid for pid, info in profiles.items()
            if info.get("status") not in ("completed", "failed", "stopped")
        ]

    def start_run(self, profile_ids: list):
        """Start tracking a new warmup run."""
        self.data = {
            "started_at": datetime.now().isoformat(),
            "profiles": {
                pid: {"status": "waiting", "started_at": None, "finished_at": None}
                for pid in profile_ids
            },
        }
        self._save()

    def get_profile_status(self, profile_id: str) -> str:
        """Get a profile's current status from progress data."""
        if not self.data:
            return ""
        return self.data.get("profiles", {}).get(profile_id, {}).get("status", "")

    def update_profile(self, profile_id: str, status: str):
        """Update a profile's status in the progress file."""
        if not self.data:
            return
        if profile_id not in self.data.get("profiles", {}):
            self.data.setdefault("profiles", {})[profile_id] = {}

        self.data["profiles"][profile_id]["status"] = status
        if status in ("starting", "phase1") and not self.data["profiles"][profile_id].get("started_at"):
            self.data["profiles"][profile_id]["started_at"] = datetime.now().isoformat()
        if status in ("completed", "failed", "stopped"):
            self.data["profiles"][profile_id]["finished_at"] = datetime.now().isoformat()
        self._save()

    def clear(self):
        """Clear progress after a completed run."""
        self.data = None
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    def _save(self):
        if self.data:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
            except (OSError, TypeError) as e:
                logger.error(f"Failed to save progress: {e}")


# ══════════════════════════════════════════════════════════════════
#  PROFILE HEALTH SCORING
# ══════════════════════════════════════════════════════════════════

class ProfileMetrics:
    """Tracks browsing metrics during warmup to calculate a health score."""

    def __init__(self, profile_id: str):
        self.profile_id = profile_id
        self.persona = ""
        self.status = ""  # tracks profile state: completed/failed/stopped
        self.started_at: float = time.time()
        self.finished_at: float = 0

        # Counters
        self.pages_visited: int = 0
        self.links_clicked: int = 0
        self.unique_domains: set = set()
        self.search_queries_performed: int = 0
        self.tabs_opened: int = 0
        self.scroll_actions: int = 0
        self.popups_dismissed: int = 0
        self.captchas_encountered: int = 0
        self.errors: int = 0
        self.cookies_count: int = 0

        # Cookie-based scoring signals (Goal 5)
        self.cookie_domains: set = set()
        self.has_google_cookies: bool = False
        self.has_youtube_cookies: bool = False
        self.third_party_cookie_domains: int = 0
        self.session_cookies: int = 0
        self.persistent_cookies: int = 0
        self.score_target_domain: str = ""
        self.target_cookie_count: int = 0
        self.has_target_cookies: bool = False
        self.has_meta_pixel: bool = False
        self.has_ga_cookies: bool = False
        self.pixel_cookie_names: list = []

        # Lists for reporting
        self.visited_urls: list = []
        self.queries_used: list = []
        self.failed_sites: list = []

    MAX_VISITED_URLS = 500

    def record_page_visit(self, url: str):
        self.pages_visited += 1
        self.visited_urls.append(url)
        if len(self.visited_urls) > self.MAX_VISITED_URLS:
            self.visited_urls = self.visited_urls[-self.MAX_VISITED_URLS:]
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            if domain:
                self.unique_domains.add(domain)
        except Exception:
            pass

    def record_link_click(self):
        self.links_clicked += 1

    MAX_QUERIES_USED = 200  # Cap to prevent unbounded memory growth

    def record_search(self, query: str):
        self.search_queries_performed += 1
        self.queries_used.append(query)
        if len(self.queries_used) > self.MAX_QUERIES_USED:
            self.queries_used = self.queries_used[-self.MAX_QUERIES_USED:]

    def record_scroll(self):
        self.scroll_actions += 1

    def record_tab(self):
        self.tabs_opened += 1

    def record_popup_dismissed(self):
        self.popups_dismissed += 1

    def record_captcha(self):
        self.captchas_encountered += 1

    def record_error(self, site: str = ""):
        self.errors += 1
        if site:
            self.failed_sites.append(site)

    @staticmethod
    def _cookie_on_target(cookie_domain: str, target: str) -> bool:
        d = (cookie_domain or "").lstrip(".").lower()
        t = (target or "").lstrip(".").lower().replace("www.", "")
        if not d or not t:
            return False
        return d == t or d.endswith("." + t) or t.endswith("." + d) or t in d

    def analyze_cookies(self, cookies: list, visited_domains: set = None,
                        target_domain: str = ""):
        """Analyze browser cookies for scoring signals.
        cookies: list of cookie dicts from context.cookies() — each has
                 'domain', 'name', 'expires', 'httpOnly', 'secure', etc.
        """
        self.cookies_count = len(cookies)
        self.score_target_domain = (target_domain or "").lstrip(".").lower().replace(
            "www.", ""
        )
        self.target_cookie_count = 0
        self.has_target_cookies = False
        self.has_meta_pixel = False
        self.has_ga_cookies = False
        self.has_google_cookies = False
        self.has_youtube_cookies = False
        pixel_names = []
        domains = set()
        session_count = 0
        persistent_count = 0
        _PIXEL = {"_fbp", "_fbc"}
        _GA = {"_ga", "_gid", "_gat"}
        for c in cookies:
            domain = c.get("domain", "").lstrip(".")
            name = str(c.get("name") or "")
            domains.add(domain)
            if "google" in domain:
                self.has_google_cookies = True
            if "youtube" in domain:
                self.has_youtube_cookies = True
            if self._cookie_on_target(domain, self.score_target_domain):
                self.target_cookie_count += 1
                self.has_target_cookies = True
            if name in _PIXEL:
                self.has_meta_pixel = True
                pixel_names.append(name)
            if name in _GA:
                self.has_ga_cookies = True
                pixel_names.append(name)
            expires = c.get("expires", -1)
            if expires <= 0:
                session_count += 1
            else:
                persistent_count += 1
        seen = set()
        self.pixel_cookie_names = [
            n for n in pixel_names if not (n in seen or seen.add(n))
        ]
        self.cookie_domains = domains
        self.session_cookies = session_count
        self.persistent_cookies = persistent_count
        if visited_domains:
            third_party = domains - visited_domains
            self.third_party_cookie_domains = len(third_party)

    def cookie_log_line(self) -> str:
        """Short log line: target-host cookies and pixel names, not ads-ready claims."""
        flags = list(self.pixel_cookie_names)
        extra = f" (incl. {', '.join(flags)})" if flags else ""
        target = self.score_target_domain
        if target:
            return (
                f"Cookies: {self.target_cookie_count} on {target}{extra}, "
                f"{self.cookies_count} total, {len(self.cookie_domains)} domains"
            )
        extra = f", pixels={', '.join(flags)}" if flags else ""
        return (
            f"Cookies: {self.cookies_count} total, "
            f"{len(self.cookie_domains)} domains{extra}"
        )

    def _target_pages_visited(self) -> int:
        target = (self.score_target_domain or "").lower()
        if not target:
            return 0
        n = 0
        for url in self.visited_urls:
            if target in (url or "").lower():
                n += 1
        return n

    def finish(self):
        self.finished_at = time.time()

    @property
    def duration_s(self) -> float:
        """Duration in seconds."""
        end = self.finished_at if self.finished_at else time.time()
        return end - self.started_at

    @property
    def duration_minutes(self) -> float:
        return self.duration_s / 60

    def health_score(self) -> int:
        """
        Calculate a 0-100 readiness score.

        With a target host: pages and cookies on that site, plus pixel names
        (_fbp/_fbc/_ga). Google/YouTube cookie count is not the prize.

        Without a target: generic activity, with only a small Google/YouTube nod.
        """
        score = 0.0
        target = self.score_target_domain

        if target:
            score += min(self._target_pages_visited() * 3, 24)
            score += min(self.links_clicked, 8)
            score += min(self.duration_minutes / 3, 10)
            score += min(self.scroll_actions / 5, 5)
            score += min(self.target_cookie_count, 12)
            if self.has_meta_pixel:
                score += 10
            if self.has_ga_cookies:
                score += 5
            score += min(self.persistent_cookies / 10, 5)
            score += min(self.cookies_count / 10, 4)
        else:
            score += min(self.pages_visited, 15)
            score += min(len(self.unique_domains) * 2, 12)
            score += min(self.links_clicked, 8)
            score += min(self.search_queries_performed * 2, 8)
            score += min(self.duration_minutes / 3, 10)
            score += min(self.tabs_opened * 2, 7)
            score += min(self.cookies_count / 5, 8)
            score += min(len(self.cookie_domains), 7)
            if self.has_google_cookies:
                score += 1
            if self.has_youtube_cookies:
                score += 1
            score += min(self.persistent_cookies / 10, 5)
            score += min(self.scroll_actions / 5, 5)

        score -= self.captchas_encountered * 5
        score -= self.errors * 2

        return max(0, min(100, int(score)))

    def score_label(self) -> str:
        """Human-readable label for the health score."""
        s = self.health_score()
        if s >= 85:
            return "Excellent"
        elif s >= 70:
            return "Good"
        elif s >= 50:
            return "Fair"
        elif s >= 30:
            return "Weak"
        else:
            return "Poor"

    def to_dict(self) -> dict:
        """Export metrics as a dict for reporting."""
        return {
            "profile_id": self.profile_id,
            "persona": self.persona,
            "health_score": self.health_score(),
            "score_label": self.score_label(),
            "duration_minutes": round(self.duration_minutes, 1),
            "pages_visited": self.pages_visited,
            "unique_domains": len(self.unique_domains),
            "links_clicked": self.links_clicked,
            "search_queries": self.search_queries_performed,
            "tabs_opened": self.tabs_opened,
            "scroll_actions": self.scroll_actions,
            "popups_dismissed": self.popups_dismissed,
            "captchas": self.captchas_encountered,
            "cookies": self.cookies_count,
            "cookie_domains": len(self.cookie_domains),
            "google_cookies": self.has_google_cookies,
            "youtube_cookies": self.has_youtube_cookies,
            "target_domain": self.score_target_domain,
            "target_cookies": self.target_cookie_count,
            "has_meta_pixel": self.has_meta_pixel,
            "has_ga_cookies": self.has_ga_cookies,
            "pixel_cookies": list(self.pixel_cookie_names),
            "persistent_cookies": self.persistent_cookies,
            "session_cookies": self.session_cookies,
            "errors": self.errors,
            "visited_urls": self.visited_urls,
            "queries_used": self.queries_used,
            "failed_sites": self.failed_sites,
        }
