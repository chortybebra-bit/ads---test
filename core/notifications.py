"""
Windows toast notification helper (winotify).
Gracefully degrades if winotify is not installed or notifications are blocked at OS level.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

try:
    from winotify import Notification
    _WINOTIFY_AVAILABLE = True
except ImportError:
    _WINOTIFY_AVAILABLE = False
    logger.debug("[Notifications] winotify not installed — toast notifications disabled")

APP_ID = "AdsPower.WarmupManager"

_ICON_PATH = ""


def _get_icon() -> str:
    """Return absolute path to the app icon if it exists, else empty string."""
    global _ICON_PATH
    if _ICON_PATH:
        return _ICON_PATH
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.ico"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.png"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "icon.ico"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            _ICON_PATH = c
            return _ICON_PATH
    return ""


def send_toast(title: str, message: str, duration: str = "short") -> bool:
    """Show a Windows toast notification (no sound).

    Returns True if dispatch succeeded, False otherwise.
    Never raises — always degrades gracefully.
    duration: 'short' (5 s) or 'long' (25 s).
    """
    if not _WINOTIFY_AVAILABLE:
        logger.debug("[Notifications] Toast skipped (winotify unavailable): %s", title)
        return False

    def _send():
        try:
            toast = Notification(
                app_id=APP_ID,
                title=title,
                msg=message,
                duration=duration,
                icon=_get_icon(),
            )
            toast.show()
            logger.debug("[Notifications] Toast sent: %s", title)
        except Exception:
            logger.warning("[Notifications] Toast failed", exc_info=True)

    threading.Thread(target=_send, daemon=True).start()
    return True


# ── Convenience helpers ──────────────────────────────────────────

def notify_captcha_required(profile_id: str) -> bool:
    return send_toast(
        title="⚠ Manual CAPTCHA Required",
        message=f"Profile {profile_id}: Google CAPTCHA detected.\nSolve it in the browser to continue.",
        duration="long",
    )


def notify_captcha_resolved(profile_id: str) -> bool:
    return send_toast(
        title="✓ CAPTCHA Resolved",
        message=f"Profile {profile_id}: CAPTCHA cleared. Warmup resumed.",
        duration="short",
    )


def notify_profile_failed(profile_id: str, total_failures: int) -> bool:
    return send_toast(
        title="✗ Profile Failed",
        message=f"Profile #{profile_id} failed ({total_failures} total failures).",
        duration="short",
    )


def notify_profile_completed(profile_id: str) -> bool:
    return send_toast(
        title="✓ Profile Completed",
        message=f"Profile {profile_id} warmup finished successfully.",
        duration="short",
    )


def notify_all_done(completed: int, failed: int) -> bool:
    return send_toast(
        title="🏁 All Profiles Done",
        message=f"Completed: {completed}  |  Failed: {failed}",
        duration="long",
    )


_ERROR_MESSAGES = {
    "ERR_SOCKS_CONNECTION_FAILED": (
        "SOCKS proxy connection failed.",
        "Check proxy host, port, and credentials.",
    ),
    "ERR_PROXY_CONNECTION_FAILED": (
        "HTTP proxy connection failed.",
        "Check proxy host, port, and credentials.",
    ),
    "ERR_TUNNEL_CONNECTION_FAILED": (
        "Proxy tunnel could not be established.",
        "Proxy may be down or blocking HTTPS tunnels.",
    ),
    "ERR_TIMED_OUT": (
        "Connection timed out.",
        "Site took too long to respond. Check internet speed or proxy.",
    ),
    "ERR_CONNECTION_TIMED_OUT": (
        "Connection timed out.",
        "Site took too long to respond. Check internet speed or proxy.",
    ),
    "ERR_NAME_NOT_RESOLVED": (
        "DNS lookup failed — domain not found.",
        "Check DNS settings, proxy, or internet connection.",
    ),
    "ERR_CONNECTION_REFUSED": (
        "Connection refused by the remote server.",
        "Site may be down, or a firewall is blocking it.",
    ),
    "ERR_CONNECTION_RESET": (
        "Connection was reset.",
        "Network interrupted the transfer. Check proxy stability.",
    ),
    "ERR_INTERNET_DISCONNECTED": (
        "No internet connection detected.",
        "Check Wi-Fi / Ethernet and adapter settings.",
    ),
    "ERR_NETWORK_CHANGED": (
        "Network changed during connection.",
        "Wi-Fi or VPN switched mid-request.",
    ),
    "ERR_CONNECTION_CLOSED": (
        "Connection closed unexpectedly.",
        "Remote server or proxy dropped the connection.",
    ),
    "ERR_EMPTY_RESPONSE": (
        "Empty response from server.",
        "Proxy or server closed without sending data.",
    ),
}


def _parse_error(error_detail: str) -> tuple:
    """Extract known error code from a Playwright error string.
    Returns (code, human_description, advice)."""
    for code, (desc, advice) in _ERROR_MESSAGES.items():
        if code in error_detail:
            return code, desc, advice
    return "ERR_UNKNOWN", "Connection failed.", "Check proxy, firewall, or network."


def notify_no_internet(profile_id: str, url: str = "", error_detail: str = "") -> None:
    """Send two toasts: Chrome-style error + human-readable advice."""
    site = url[:80] if url else "unknown site"
    code, description, advice = _parse_error(error_detail)

    send_toast(
        title="This site can't be reached",
        message=(
            f"{site}\n"
            f"{description}\n"
            f"{code}"
        ),
        duration="long",
    )

    send_toast(
        title=f"⚠ Profile {profile_id} — Connection Failed",
        message=f"{advice}",
        duration="long",
    )


def notify_internet_paused(active_count: int) -> bool:
    return send_toast(
        title="⏸ All profiles paused — No Internet",
        message=(
            f"{active_count} profile(s) paused due to repeated connection failures.\n"
            "Checking connectivity... will auto-resume when back online."
        ),
        duration="long",
    )


def notify_internet_resumed(active_count: int) -> bool:
    return send_toast(
        title="▶ Internet restored — Resuming",
        message=f"{active_count} profile(s) resuming warmup.",
        duration="short",
    )


def notify_cloudflare_captcha(profile_id: str, url: str = "") -> bool:
    site = url[:80] if url else "unknown site"
    return send_toast(
        title="⚠ Cloudflare Challenge Detected",
        message=f"Profile {profile_id}: Cloudflare is blocking access.\n{site}\nWaiting for auto-resolve...",
        duration="long",
    )


def notify_test() -> bool:
    """Lightweight test toast triggered by user action."""
    return send_toast(
        title="AdsPower Warmup Manager",
        message="Notifications are working!",
        duration="short",
    )
