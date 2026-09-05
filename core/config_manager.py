"""Configuration manager - load, save, and validate settings."""

import json
import os
import copy
import logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "adspower_url": "http://local.adspower.net:50325",
    "profiles": [],
    "max_concurrent": 20,
    "session_minutes": 45,
    "sites_per_profile": 25,
    "persona_mode": "Skin Trader",
    "target_website": "",
    "target_warmup_enabled": False,
    "youtube_enabled": True,
    "bandwidth_saver": False,
    "captcha_enabled": True,
    "captcha_service": "2captcha",
    "captcha_api_key": "",
    "windows_notifications_enabled": True,
    "launch_delay_min": 10,
    "launch_delay_max": 30,
    "launch_batch_size": 3,
    "launch_batch_cooldown": 15,
    "sites": [
        "https://www.google.com",
        "https://steamcommunity.com/market/",
        "https://store.steampowered.com",
        "https://backpack.tf",
        "https://marketplace.tf",
        "https://wiki.teamfortress.com/wiki/Main_Page",
        "https://www.reddit.com/r/tf2/",
        "https://www.reddit.com/r/tf2trading/",
        "https://www.reddit.com/r/playrust/",
        "https://rust.tm",
        "https://rustskins.gg",
    ],
    "search_queries": [
        "tf2 key price 2026",
        "best site to buy tf2 items",
        "tf2 unusual hat price checker",
        "backpack.tf unusual classifieds",
        "how to sell tf2 items for real money",
        "marketplace.tf key price",
        "is mannco.store legit",
        "tf2 australium price",
        "rust skins steam market",
        "best site to buy rust skins 2026",
        "rust ak skin prices",
        "rust item store rotation",
    ],
    "timing": {
        "action_delay_min": 2,
        "action_delay_max": 10,
        "scroll_min_px": 300,
        "scroll_max_px": 900,
        "idle_min_minutes": 5,
        "idle_max_minutes": 12,
        "tabs_min": 3,
        "tabs_max": 5,
        "typing_delay_min_ms": 35,
        "typing_delay_max_ms": 150,
        "dwell_min_s": 2,
        "dwell_max_s": 5,
        "page_load_timeout_ms": 20000,
    },
}


class ConfigManager:
    """Manages application configuration with JSON persistence."""

    def __init__(self, path=None):
        if path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base_dir, "config.json")
        self.path = path
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        """Load config from disk, merging with defaults for missing keys."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._merge(self.data, saved)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load config from {self.path}: {e} — using defaults")

        self.validate()

    def save(self) -> bool:
        """Persist current config to disk. Returns True on success."""
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            return True
        except (OSError, TypeError) as e:
            logger.error(f"Failed to save config: {e}")
            return False

    def get(self, key, default=None):
        """Get a top-level config value."""
        return self.data.get(key, default)

    def set(self, key, value):
        """Set a top-level config value (call save() when done with batch changes)."""
        self.data[key] = value

    def get_timing(self, key, default=None):
        """Get a timing sub-config value."""
        return self.data.get("timing", {}).get(key, default)

    def set_timing(self, key, value):
        """Set a timing sub-config value (call save() when done with batch changes)."""
        if "timing" not in self.data:
            self.data["timing"] = {}
        self.data["timing"][key] = value

    def validate(self) -> list:
        """Validate config values and return a list of warnings (empty = valid)."""
        warnings = []
        d = self.data

        # max_concurrent must be >= 1
        mc = d.get("max_concurrent", 20)
        if not isinstance(mc, int) or mc < 1:
            warnings.append(f"max_concurrent must be >= 1 (got {mc})")
            d["max_concurrent"] = max(1, int(mc) if isinstance(mc, (int, float)) else 20)

        # sites_per_profile must be >= 1
        spp = d.get("sites_per_profile", 15)
        if not isinstance(spp, int) or spp < 1:
            warnings.append(f"sites_per_profile must be >= 1 (got {spp})")
            d["sites_per_profile"] = max(1, int(spp) if isinstance(spp, (int, float)) else 15)

        # launch delays must be positive and min <= max
        ld_min = d.get("launch_delay_min", 10)
        ld_max = d.get("launch_delay_max", 30)
        if isinstance(ld_min, (int, float)) and isinstance(ld_max, (int, float)):
            if ld_min > ld_max:
                warnings.append(f"launch_delay_min ({ld_min}) > launch_delay_max ({ld_max}) — swapped")
                d["launch_delay_min"], d["launch_delay_max"] = ld_max, ld_min

        # Dear PyGui saves some fields as floats; randint() requires ints.
        timing = d.get("timing", {})
        for key in ("scroll_min_px", "scroll_max_px", "tabs_min", "tabs_max"):
            val = timing.get(key)
            if val is not None:
                try:
                    timing[key] = int(val)
                except (TypeError, ValueError):
                    warnings.append(f"timing.{key} is not a number (got {val})")

        # session_minutes drives site count and depth (15–120)
        sm = d.get("session_minutes", 45)
        try:
            sm = int(sm)
        except (TypeError, ValueError):
            sm = 45
            warnings.append("session_minutes is not a number — using 45")
        if sm < 15 or sm > 120:
            warnings.append(f"session_minutes must be 15–120 (got {sm})")
            sm = max(15, min(120, sm))
        d["session_minutes"] = sm

        for dead_key in ("warmup_speed", "depth_multiplier", "deep_sites"):
            d.pop(dead_key, None)

        # timing values must be positive
        for key in ("action_delay_min", "action_delay_max", "typing_delay_min_ms",
                     "typing_delay_max_ms", "page_load_timeout_ms"):
            val = timing.get(key)
            if val is not None and (not isinstance(val, (int, float)) or val < 0):
                warnings.append(f"timing.{key} must be >= 0 (got {val})")

        for w in warnings:
            logger.warning(f"Config validation: {w}")

        return warnings

    @staticmethod
    def _merge(base: dict, override: dict):
        """Recursively merge override into base (in-place)."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigManager._merge(base[key], value)
            else:
                base[key] = value
