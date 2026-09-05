"""Human behavior simulation - realistic browsing with full edge-case handling."""

import asyncio
import random
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse, quote_plus

from .captcha_solver import CaptchaSolver

logger = logging.getLogger(__name__)


class HumanSimulator:
    """Simulates human-like browsing behavior to avoid bot detection."""

    # Playwright error substrings that indicate a network / connectivity problem
    _NETWORK_ERROR_PATTERNS = (
        "net::ERR_TIMED_OUT",
        "net::ERR_CONNECTION_TIMED_OUT",
        "net::ERR_NAME_NOT_RESOLVED",
        "net::ERR_CONNECTION_REFUSED",
        "net::ERR_CONNECTION_RESET",
        "net::ERR_INTERNET_DISCONNECTED",
        "net::ERR_NETWORK_CHANGED",
        "net::ERR_PROXY_CONNECTION_FAILED",
        "net::ERR_TUNNEL_CONNECTION_FAILED",
        "net::ERR_SOCKS_CONNECTION_FAILED",
        "net::ERR_CONNECTION_CLOSED",
        "net::ERR_EMPTY_RESPONSE",
        "Timeout",
    )

    def __init__(self, timing_config: dict, activity_cb=None, skip_event=None,
                 pause_check=None, bandwidth_saver: bool = False,
                 captcha_solver=None, stop_check=None,
                 manual_captcha_cb=None, network_error_cb=None,
                 nav_success_cb=None, search_gate=None,
                 google_blocked_initial: bool = False,
                 on_google_blocked=None, youtube_enabled: bool = True,
                 interest_keywords=None, link_bias=None,
                 google_budget: int = None, proxy_config: dict = None,
                 profile_id: str = "", refresh_proxy=None):
        self.timing = timing_config
        self._activity_cb = activity_cb  # Optional callback: activity_cb(text)
        self._skip_event = skip_event    # threading.Event — set to skip current work
        self._pause_check = pause_check  # async callable — blocks while paused
        self._bandwidth_saver = bandwidth_saver  # True = context already blocks media
        self._captcha_solver = captcha_solver  # CaptchaSolver instance (or None)
        self._stop_check = stop_check    # Callable that returns True if stop requested
        self._proxy_config = dict(proxy_config or {})
        self._profile_id = str(profile_id or "")
        self._refresh_proxy = refresh_proxy  # async () -> dict, AdsPower re-fetch
        # Callback for CAPTCHA / Cloudflare events: cb(event_type, details_dict)
        self._manual_captcha_cb = manual_captcha_cb
        # Callback when a network/connectivity error is detected: cb(url, error_str)
        self._network_error_cb = network_error_cb
        # Callback on successful navigation — used to clear error strike counters
        self._nav_success_cb = nav_success_cb
        self._google_warmed_up = False   # Track if Google session has been warmed up
        self._last_google_search_time = 0  # Track time between searches (avoid too frequent)

        # ── Anti-detection: search-engine exposure control ──
        self._search_gate = search_gate          # cross-profile stagger (SearchGate)
        self._on_google_blocked = on_google_blocked  # cb() to persist a Google block
        # Per-session Google budget: enough searches to land 25 unique hosts.
        self._google_search_count = 0
        if google_budget is not None:
            self._google_budget = max(1, int(google_budget))
        else:
            self._google_budget = random.randint(18, 28)
        # If this profile was CAPTCHA'd recently, avoid Google entirely this run.
        self._google_blocked = bool(google_blocked_initial)
        self._youtube_enabled = bool(youtube_enabled)

        # ── Human decision-making: per-session fatigue + interests ──
        # A real person tires over a session (slower, longer pauses) and has
        # personal interests that bias what they click. Each profile is different.
        self._session_start = time.monotonic()
        self._session_span_s = random.uniform(20 * 60, 75 * 60)  # perceived session length
        self._link_bias = dict(link_bias or {})
        _default_pool = [
            "unusual", "unusuals", "hat", "hats", "keys", "key",
            "australium", "strange", "refined", "metal", "effect",
            "ak", "door", "rust", "skins", "tf2", "loadout",
            "knife", "karambit", "gloves", "cs2", "awp",
        ]
        _interest_pool = list(interest_keywords) if interest_keywords else _default_pool
        random.shuffle(_interest_pool)
        n_pick = min(len(_interest_pool), random.randint(2, 5))
        self._interests = set(_interest_pool[:n_pick])
        # Target marketplace host — always allowed to use Google (budget does not apply).
        self._target_host = ""
        self._skip_auth_forms = False
        self._cart_tried_hosts = set()

    def apply_persona_style(self, interest_keywords=None, link_bias=None,
                            skip_auth_forms: bool = None):
        """Re-seed click interests / link scoring from the assigned persona."""
        if link_bias is not None:
            self._link_bias = dict(link_bias)
        if interest_keywords:
            pool = list(interest_keywords)
            random.shuffle(pool)
            n_pick = min(len(pool), random.randint(2, 5))
            self._interests = set(pool[:n_pick])
        if skip_auth_forms is not None:
            self._skip_auth_forms = bool(skip_auth_forms)

    def _link_priority_map(self) -> dict:
        prio = dict(self._LINK_PRIORITY)
        for kw, extra in (self._link_bias or {}).items():
            prio[kw] = max(prio.get(kw, 0), int(extra) + 7)
        return prio

    def _is_meta_surface(self, page) -> bool:
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        return any(h in url for h in (
            "instagram.com", "facebook.com", "meta.com", "messenger.com",
        ))

    def _should_skip_auth_ui(self, page) -> bool:
        return bool(getattr(self, "_skip_auth_forms", False)) or self._is_meta_surface(page)

    def _check_stop(self):
        """Check if a hard stop has been requested - raises _StopRequested to halt immediately."""
        if self._stop_check and self._stop_check():
            raise _StopRequested()

    def _check_skip(self):
        """Check if a skip has been requested for this profile."""
        self._check_stop()  # Always check stop first
        if self._skip_event and self._skip_event.is_set():
            self._skip_event.clear()  # Reset so it can be used again
            raise _SkipPhase()

    def _is_network_error(self, exc: Exception) -> bool:
        """Return True if the exception looks like a network / connectivity failure."""
        msg = str(exc)
        return any(p in msg for p in self._NETWORK_ERROR_PATTERNS)

    def _fire_network_error(self, url: str, exc: Exception):
        """Notify engine about a network connectivity problem (non-blocking)."""
        if self._network_error_cb:
            try:
                self._network_error_cb(url, str(exc))
            except Exception:
                pass

    def _report(self, text: str, important: bool = False):
        """
        Report current activity to the UI (if callback set).
        If important=True, also logs to console for debugging.
        Auto-detects important messages by keywords.
        """
        if self._activity_cb:
            try:
                self._activity_cb(text)
            except Exception:
                pass
        
        # Auto-detect important messages
        text_lower = text.lower()
        is_important = important or any(kw in text_lower for kw in [
            "captcha", "error", "failed", "blocked", "banned", "solved",
            "complete", "success", "skip", "timeout", "retry"
        ])
        
        if is_important:
            logger.info(f"[ACTIVITY] {text}")

    async def _ensure_tab_visible(self, page):
        """
        Make this the active tab inside the browser WITHOUT bringing the
        browser window to the foreground / un-minimizing it.

        Uses CDP Target.activateTarget which only switches the active tab
        internally. Falls back to a JS focus() call which also avoids
        window-level focus stealing.
        """
        try:
            client = await page.context.new_cdp_session(page)
            try:
                info = await client.send("Target.getTargetInfo")
                target_id = info.get("targetInfo", {}).get("targetId")
                if target_id:
                    await client.send("Target.activateTarget",
                                      {"targetId": target_id})
                else:
                    await client.send("Page.bringToFront")
            finally:
                try:
                    await client.detach()
                except Exception:
                    pass
        except Exception:
            try:
                await page.evaluate("() => { try { window.focus(); } catch(e) {} }")
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════
    #  POPUP / OVERLAY / COOKIE HANDLING
    # ══════════════════════════════════════════════════════════════

    async def dismiss_popups(self, page):
        """
        Safely dismiss all popups, modals, banners, and overlays.

        Strategy (ordered by safety):
        1. Press Escape (safest — closes most modals without side effects)
        2. Click safe text-based dismiss buttons ("No thanks", "Maybe later")
        3. Click cookie accept buttons
        4. Click close buttons ONLY if inside a modal container
        5. Never click X buttons that might close the tab/window
        """
        dismissed_any = False

        # Step 1: Escape key — safest way to close modals
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # Step 2: Cookie consent banners (safe to accept — also builds realistic profile)
        dismissed_any |= await self._accept_cookies(page)

        # Step 3: Promotional popups — click safe dismiss text buttons
        dismissed_any |= await self._dismiss_promo_popups(page)

        # Step 4: If overlays still exist, try clicking the backdrop
        dismissed_any |= await self._click_backdrop(page)

        # Step 5: Second Escape in case new dialogs appeared
        if dismissed_any:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        return dismissed_any

    async def _handle_google_consent(self, page) -> bool:
        """Handle Google's 'Before you continue' / cookie consent screen.
        Google's consent page uses no standard cookie/gdpr class names — needs special handling.
        Clicks 'Accept all' (or translated equivalent). Returns True if dismissed."""
        try:
            # Only trigger when actually on Google's consent/cookie screen.
            # Google consent shows at google.com itself (before search) or consent.google.com
            url = page.url.lower()
            on_google = "google." in url
            if not on_google:
                return False
            # Quick check: is the consent overlay actually present?
            has_consent = await page.query_selector(
                'form[action*="consent"], div[jsname][class*="consent"], '
                'button:has-text("Accept all"), button:has-text("Aceptar todo"), '
                'button:has-text("Alle akzeptieren")'
            )
            if not has_consent:
                return False

            # Google consent button selectors (30+ locales)
            google_accept_texts = [
                "Accept all", "Aceptar todo",           # EN, ES
                "Alle akzeptieren", "Tout accepter",    # DE, FR
                "Accetta tutto", "Alles accepteren",    # IT, NL
                "Aceitar tudo", "Принять все",          # PT, RU
                "Tümünü kabul et", "Kabul et",          # TR
                "Zaakceptuj wszystko",                  # PL
                "Souhlasit se vším", "Přijmout vše",    # CZ
                "Acceptera alla",                       # SE
                "Aksepter alle",                        # NO
                "Accepter alle",                        # DK
                "Hyväksy kaikki",                       # FI
                "Összes elfogadása",                    # HU
                "Αποδοχή όλων",                         # GR
                "Прийняти все",                         # UA
                "Приеми всички",                        # BG
                "Acceptă tot",                          # RO
                "قبول الكل",                              # AR
                "קבל הכל",                               # HE
                "सभी स्वीकार करें",                          # HI
                "すべて許可", "すべて受け入れる",              # JA
                "모두 허용", "모두 수락",                    # KO
                "ยอมรับทั้งหมด",                           # TH
                "Chấp nhận tất cả",                     # VI
                "Terima semua",                         # ID/MS
            ]
            accept_selectors = [
                *[f'button:has-text("{t}")' for t in google_accept_texts],
                # Generic fallback: the right/primary button in Google's consent form
                'form[action*="consent"] button[jsname]',
                'div[class*="consent"] button',
                '#L2AGLb',    # Google's "Accept all" button ID (historic)
                'button[jsname="higCR"]',
            ]

            for sel in accept_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        logger.debug(f"[Google consent] Dismissed via: {sel}")
                        return True
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"[Google consent] handler error: {e}")
        return False

    async def _accept_cookies(self, page):
        """Find and click cookie consent accept buttons — very aggressive matching."""
        # First: handle Google's own consent screen (special case — no standard classes)
        if await self._handle_google_consent(page):
            return True

        # CSS selectors for cookie banner containers
        cookie_container_selectors = [
            "[id*='cookie']", "[class*='cookie']", "[class*='Cookie']",
            "[id*='consent']", "[class*='consent']", "[class*='Consent']",
            "[id*='gdpr']", "[class*='gdpr']", "[class*='GDPR']",
            "[id*='privacy']", "[class*='privacy-banner']",
            "[class*='cc-banner']", "[class*='cc_banner']",
            "[class*='CookieBanner']", "[class*='cookieBanner']",
            "[class*='cookie-bar']", "[class*='cookie_bar']",
            "[class*='cookie-notice']", "[class*='cookie_notice']",
            "[class*='notice-banner']", "[class*='js-cookie']",
            "[data-testid*='cookie']", "[data-testid*='consent']",
            "[aria-label*='cookie']", "[aria-label*='consent']",
            "#onetrust-banner-sdk", "#onetrust-consent-sdk",
            ".onetrust-pc-dark-filter", "#CybotCookiebotDialog",
            "#usercentrics-root", ".fc-consent-root",
            "[class*='osano']", "#sp_message_container",
            "#truste-consent-track", ".evidon-banner",
            "[class*='didomi']", "#didomi-host",
        ]

        # Safe button texts to click inside cookie banners.
        # Covers 30+ languages for worldwide profile support.
        accept_texts = [
            # English
            "accept all", "accept cookies", "accept", "allow all cookies",
            "allow all", "allow cookies", "agree to all", "agree",
            "i agree", "i accept", "got it", "ok", "okay", "allow",
            "understood", "continue", "confirm", "yes", "yes, i agree",
            "consent", "enable all", "allow selection", "save and close",
            "that's ok", "that's okay", "alright", "sounds good",
            "i understand", "acknowledge",
            # Russian
            "принять все", "принять", "согласен", "согласиться",
            "разрешить все", "разрешить", "хорошо", "понятно",
            # Spanish
            "aceptar todo", "aceptar todas", "aceptar", "acepto",
            "permitir todo", "permitir todas", "permitir",
            "de acuerdo", "entendido", "continuar",
            # German
            "alle akzeptieren", "akzeptieren", "zustimmen",
            "einverstanden", "alle zulassen", "erlauben",
            "einstellungen speichern", "verstanden",
            # French
            "tout accepter", "accepter tout", "accepter",
            "j'accepte", "j'ai compris", "autoriser",
            "continuer", "d'accord",
            # Italian
            "accetta tutto", "accetta tutti", "accetta", "accetto",
            "acconsento", "consenti", "ho capito",
            # Portuguese
            "aceitar tudo", "aceitar todos", "aceitar", "aceito",
            "concordo", "permitir tudo", "permitir",
            # Dutch
            "alles accepteren", "accepteren", "akkoord",
            "alle cookies accepteren", "begrepen", "toestaan",
            # Polish
            "zaakceptuj wszystko", "zaakceptuj", "akceptuję",
            "zgadzam się", "rozumiem", "zgoda",
            # Czech
            "přijmout vše", "přijmout", "souhlasím",
            "povolit vše", "rozumím",
            # Turkish
            "tümünü kabul et", "kabul et", "hepsini kabul et",
            "kabul ediyorum", "tamam", "anladım",
            # Arabic
            "قبول الكل", "قبول", "موافق", "أوافق",
            "قبول جميع ملفات تعريف الارتباط", "موافقة",
            "حسناً", "حسنا", "تم",
            # Hebrew
            "קבל הכל", "קבל", "אישור", "מסכים",
            "אני מסכים", "הבנתי", "אוקיי",
            # Hindi
            "सभी स्वीकार करें", "स्वीकार करें", "स्वीकार",
            "सहमत", "ठीक है", "अनुमति दें", "समझ गया",
            # Japanese
            "すべて許可", "すべて受け入れる", "同意する",
            "許可する", "承認", "了解",
            # Korean
            "모두 허용", "모두 수락", "동의", "수락",
            "허용", "확인", "동의합니다",
            # Thai
            "ยอมรับทั้งหมด", "ยอมรับ", "ตกลง", "อนุญาต",
            # Vietnamese
            "chấp nhận tất cả", "chấp nhận", "đồng ý", "cho phép",
            # Indonesian / Malay
            "terima semua", "terima", "setuju", "izinkan",
            # Swedish
            "acceptera alla", "acceptera", "godkänn",
            # Norwegian
            "aksepter alle", "aksepter", "godta",
            # Danish
            "accepter alle", "accepter", "tillad alle",
            # Finnish
            "hyväksy kaikki", "hyväksy", "salli kaikki",
            # Romanian
            "acceptă tot", "acceptă", "de acord",
            # Hungarian
            "összes elfogadása", "elfogadás", "elfogadom",
            # Greek
            "αποδοχή όλων", "αποδοχή", "συμφωνώ",
            # Ukrainian
            "прийняти все", "прийняти", "погоджуюсь", "дозволити",
            # Bulgarian
            "приеми всички", "приемам", "съгласен съм",
        ]

        try:
            # Strategy 1: Search inside known cookie containers
            for container_sel in cookie_container_selectors:
                containers = await page.query_selector_all(container_sel)
                for container in containers:
                    try:
                        if not await container.is_visible():
                            continue
                        buttons = await container.query_selector_all(
                            "button, a, [role='button'], input[type='button'], "
                            "input[type='submit'], span[role='button']"
                        )
                        for btn in buttons:
                            try:
                                text = (await btn.inner_text() or "").strip().lower()
                                if any(t in text for t in accept_texts):
                                    await btn.click()
                                    logger.debug(f"Accepted cookies: '{text}'")
                                    await asyncio.sleep(0.5)
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Strategy 2: Direct button search by common IDs/classes
            direct_selectors = [
                # OneTrust
                "#onetrust-accept-btn-handler",
                "#accept-recommended-btn-handler",
                ".onetrust-close-btn-handler",
                # Cookiebot (all variants)
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                "#CybotCookiebotDialogBodyButtonAccept",
                "#CybotCookiebotDialogBodyLevelButtonAccept",
                "#CybotCookiebotDialogBodyButtonDecline",  # Sometimes only decline visible
                "a.CybotCookiebotDialogBodyLink",
                "#CookiebotDialogAllowallButton",
                ".CybotCookiebotDialogBodyButton",
                "[data-cookiebanner='accept_button']",
                # Generic cc classes
                ".cc-accept", ".cc-allow", ".cc-dismiss", ".cc-btn.cc-allow",
                # Data attributes
                "[data-action='accept']", "[data-cookie-accept]",
                "button[data-consent='accept']",
                "[data-gdpr-consent='accept']",
                "[data-cy='cookie-banner-accept']",
                # FC consent
                ".fc-cta-consent", ".fc-button.fc-cta-consent",
                ".fc-primary-button",
                # Didomi
                ".didomi-continue-without-agreeing",
                "#didomi-notice-agree-button",
                ".didomi-components-button--color-primary",
                # Osano
                ".osano-cm-accept-all", ".osano-cm-accept",
                ".osano-cm-button--type_accept",
                # SourcePoint
                "#sp_choice_accept_all",
                "button.sp_choice_type_11",
                # Quantcast
                ".qc-cmp2-summary-buttons button.css-47sehv",
                # TrustArc
                ".trustarc-agree-btn",
                ".pdynamicbutton .call",
                # Iubenda
                ".iubenda-cs-accept-btn",
                # Generic patterns
                "[class*='accept-all']", "[class*='acceptAll']",
                "[id*='accept-all']", "[id*='acceptAll']",
                "button[title='Accept']", "button[title='Accept All']",
                "[class*='cookie-accept']", "[class*='cookieAccept']",
                "[class*='agree-button']", "[class*='agreeButton']",
            ]
            for sel in direct_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        logger.debug(f"Accepted cookies via direct selector: {sel}")
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    continue

            # Strategy 3: Any visible button on the page with accept text
            # (last resort — broader search, uses full multilingual list)
            # Exact-match subset to avoid false positives on generic words
            exact_match_texts = {
                # Only match whole button text to avoid clicking random "OK" buttons
                "accept all", "accept cookies", "accept", "allow all",
                "allow all cookies", "i agree", "agree",
                "принять все", "принять",
                "aceptar todo", "aceptar todas", "aceptar",
                "alle akzeptieren", "akzeptieren", "zustimmen",
                "tout accepter", "accepter",
                "accetta tutto", "accetta",
                "aceitar tudo", "aceitar",
                "alles accepteren", "accepteren",
                "zaakceptuj wszystko", "zaakceptuj",
                "přijmout vše", "přijmout",
                "tümünü kabul et", "kabul et",
                "قبول الكل", "قبول", "موافق",
                "קבל הכל", "קבל", "אישור",
                "सभी स्वीकार करें", "स्वीकार करें",
                "すべて許可", "すべて受け入れる", "同意する",
                "모두 허용", "모두 수락", "동의",
                "ยอมรับทั้งหมด", "ยอมรับ",
                "chấp nhận tất cả", "chấp nhận",
                "terima semua", "terima",
                "acceptera alla", "acceptera",
                "aksepter alle", "aksepter",
                "hyväksy kaikki", "hyväksy",
                "acceptă tot", "acceptă",
                "összes elfogadása", "elfogadom",
                "αποδοχή όλων", "αποδοχή",
                "прийняти все", "прийняти",
                "приеми всички", "приемам",
            }
            try:
                all_buttons = await page.query_selector_all(
                    "button, [role='button'], a.button, a.btn"
                )
                for btn in all_buttons[:40]:
                    try:
                        if not await btn.is_visible():
                            continue
                        text = (await btn.inner_text() or "").strip().lower()
                        if text in exact_match_texts:
                            await btn.click()
                            logger.debug(f"Accepted cookies (broad): '{text}'")
                            await asyncio.sleep(0.5)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Cookie accept error: {e}")
        return False

    async def _dismiss_promo_popups(self, page):
        """Dismiss promotional/registration popups using safe text buttons."""
        safe_dismiss_texts = [
            # English
            "no thanks", "no, thanks", "no thank you",
            "maybe later", "not now", "not interested",
            "dismiss", "skip", "close", "cancel",
            "remind me later", "i'll do it later",
            "continue browsing", "keep browsing",
            "continue without", "continue to site",
            "go to site", "proceed", "no, thank you",
            "not right now", "later", "decline",
            "pass", "×", "✕", "✖", "✗",
            # Russian
            "нет", "нет, спасибо", "позже", "закрыть", "пропустить",
            "не сейчас", "отклонить", "отмена",
            # Spanish
            "no, gracias", "ahora no", "más tarde", "cerrar",
            "rechazar", "omitir", "cancelar",
            # German
            "nein danke", "später", "schließen", "überspringen",
            "ablehnen", "abbrechen", "nicht jetzt",
            # French
            "non merci", "plus tard", "fermer", "passer",
            "refuser", "annuler", "pas maintenant",
            # Italian
            "no grazie", "dopo", "chiudi", "salta",
            "rifiuta", "annulla", "non ora",
            # Portuguese
            "não, obrigado", "depois", "fechar", "pular",
            "recusar", "cancelar",
            # Arabic
            "لا شكراً", "لاحقاً", "إغلاق", "تخطي", "رفض", "إلغاء",
            # Hebrew
            "לא תודה", "מאוחר יותר", "סגור", "דלג", "ביטול",
            # Hindi
            "नहीं धन्यवाद", "बाद में", "बंद करें", "छोड़ें",
            # Turkish
            "hayır teşekkürler", "daha sonra", "kapat", "atla", "reddet",
            # Japanese
            "結構です", "後で", "閉じる", "スキップ",
            # Korean
            "괜찮습니다", "나중에", "닫기", "건너뛰기",
            # Thai
            "ไม่ ขอบคุณ", "ภายหลัง", "ปิด", "ข้าม",
        ]

        overlay_selectors = [
            "[role='dialog']", "[role='alertdialog']",
            "[class*='modal']", "[class*='Modal']",
            "[class*='popup']", "[class*='Popup']", "[class*='pop-up']",
            "[class*='overlay']", "[class*='Overlay']",
            "[class*='dialog']", "[class*='Dialog']",
            "[class*='banner']", "[class*='Banner']",
            "[class*='notification']", "[class*='Notification']",
            "[class*='interstitial']", "[class*='lightbox']",
            "[class*='promo']", "[class*='Promo']",
            "[class*='signup']", "[class*='sign-up']",
            "[class*='newsletter']", "[class*='Newsletter']",
            "[data-testid*='modal']", "[data-testid*='popup']",
            "[data-testid*='dialog']", "[data-testid*='banner']",
        ]

        try:
            for overlay_sel in overlay_selectors:
                overlays = await page.query_selector_all(overlay_sel)
                for overlay in overlays:
                    try:
                        if not await overlay.is_visible():
                            continue

                        clickables = await overlay.query_selector_all(
                            "button, a, [role='button'], input[type='button'], "
                            "span[role='button'], div[role='button']"
                        )
                        for btn in clickables:
                            try:
                                text = (await btn.inner_text() or "").strip().lower()
                                if any(t in text for t in safe_dismiss_texts):
                                    await btn.click()
                                    logger.debug(f"Dismissed popup: '{text}'")
                                    await asyncio.sleep(0.4)
                                    return True
                            except Exception:
                                continue

                        # Try aria-label / data-dismiss close buttons
                        close_btns = await overlay.query_selector_all(
                            "[aria-label='Close'], [aria-label='close'], "
                            "[aria-label='Dismiss'], [aria-label='dismiss'], "
                            "[data-dismiss='modal'], [data-dismiss='alert'], "
                            "[data-close], .close-btn, .close-button, "
                            ".btn-close, .modal-close, .dialog-close, "
                            "button.close, [class*='close-icon'], "
                            "[class*='closeBtn'], [class*='close_btn']"
                        )
                        for btn in close_btns:
                            try:
                                if await btn.is_visible():
                                    await btn.click()
                                    logger.debug("Dismissed popup via close button")
                                    await asyncio.sleep(0.4)
                                    return True
                            except Exception:
                                continue

                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Popup dismiss error: {e}")
        return False

    async def _click_backdrop(self, page):
        """Click the backdrop/overlay behind a modal to close it."""
        backdrop_selectors = [
            "[class*='backdrop']", "[class*='Backdrop']",
            "[class*='overlay-bg']", "[class*='modal-overlay']",
            "[class*='mask']", "[class*='Mask']",
            "[class*='dimmer']", "[class*='shade']",
            "[class*='scrim']", "[class*='lightbox-overlay']",
            ".ReactModal__Overlay",
        ]
        try:
            for sel in backdrop_selectors:
                backdrops = await page.query_selector_all(sel)
                for bd in backdrops:
                    try:
                        if await bd.is_visible():
                            box = await bd.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + 5, box["y"] + 5)
                                await asyncio.sleep(0.4)
                                return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    # ══════════════════════════════════════════════════════════════
    #  CAPTCHA DETECTION
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    async def is_google_sorry_block(page) -> bool:
        """Return True if the page is a Google Sorry / unusual-traffic block."""
        return await CaptchaSolver.is_google_sorry_page(page)

    @staticmethod
    def _google_recaptcha_chrome_is_normal(url: str) -> bool:
        """reCAPTCHA widgets on a Google SERP/homepage are not a Sorry page."""
        u = (url or "").lower()
        if CaptchaSolver.url_looks_like_sorry(u):
            return False
        if "google." not in u and "google.com" not in u:
            return False
        if "/search" in u:
            return True
        try:
            parsed = urlparse(u)
        except Exception:
            return False
        host = (parsed.netloc or "").replace("www.", "")
        if not (host.startswith("google.") or host.endswith("google.com")
                or host == "google.com"):
            return False
        path = parsed.path or "/"
        return path in ("/", "") or path.startswith("/webhp") or path.startswith("/search")

    def _log_proxy_for_solve(self, has_creds: bool):
        pub = CaptchaSolver.proxy_public_fields(self._proxy_config)
        pid = self._profile_id or "?"
        soft = pub.get("proxy_soft") or "?"
        p_type = pub.get("proxy_type") or "?"
        host = pub.get("proxy_host") or "empty"
        port = pub.get("proxy_port") or "empty"
        if has_creds:
            logger.info(
                f"[{pid}] 2Captcha proxy: soft={soft} type={p_type} "
                f"host={host} port={port}"
            )
        else:
            logger.error(
                f"[{pid}] 2Captcha has no proxy credentials "
                f"(proxy_soft={soft}, type={p_type}, host={host}, port={port}) "
                f"— using Proxyless; Google Sorry tokens will likely fail"
            )
            self._report(
                f"No proxy credentials (soft={soft}) — 2Captcha Proxyless"
            )

    async def _ensure_proxy_for_solve(self) -> dict:
        """Use stored proxy; if unusable, re-fetch from AdsPower once."""
        proxy = CaptchaSolver.normalize_proxy(self._proxy_config)
        if proxy:
            self._log_proxy_for_solve(True)
            return self._proxy_config
        if self._refresh_proxy:
            try:
                fresh = await self._refresh_proxy()
                if fresh:
                    self._proxy_config = dict(fresh)
            except Exception as e:
                logger.warning(
                    f"[{self._profile_id or '?'}] AdsPower proxy re-fetch failed: {e}"
                )
        proxy = CaptchaSolver.normalize_proxy(self._proxy_config)
        self._log_proxy_for_solve(bool(proxy))
        return self._proxy_config

    async def _reload_sorry_for_fresh_datas(self, page):
        """Reload Sorry so Google issues a new one-shot data-s."""
        try:
            url = page.url
        except Exception:
            url = ""
        logger.info(
            f"[{self._profile_id or '?'}] Reloading Sorry page for a fresh data-s"
        )
        self._report("Reloading Sorry page for a new data-s...")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            if url:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    logger.warning(f"Sorry reload failed: {e}")
                    return
        try:
            await page.wait_for_selector(
                "iframe[src*='recaptcha'], [data-sitekey], [data-s]",
                timeout=8000,
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)

    async def detect_captcha(self, page) -> bool:
        """
        Check if the page has a captcha. Returns True if captcha found.
        Google SERP recaptcha chrome is ignored; only Sorry counts there.
        """
        try:
            page_url = page.url
            logger.debug(f"Checking for captcha on: {page_url[:80]}")
        except Exception:
            page_url = "unknown"

        try:
            if await CaptchaSolver.is_google_sorry_page(page):
                logger.info(
                    f"Google sorry/unusual traffic page detected: {page_url[:60]}"
                )
                self._report("CAPTCHA page detected (Google sorry)")
                return True
        except Exception as e:
            logger.debug(f"Google sorry check error: {e}")

        if self._google_recaptcha_chrome_is_normal(page_url):
            logger.debug(
                f"Ignoring recaptcha chrome on Google surface: {page_url[:60]}"
            )
            return False

        captcha_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='captcha']",
            ".g-recaptcha",
            ".h-captcha",
            "#captcha",
            "[class*='captcha']",
            "[id*='captcha']",
            "iframe[title*='reCAPTCHA']",
            "iframe[title*='hCaptcha']",
        ]
        try:
            for sel in captcha_selectors:
                element = await page.query_selector(sel)
                if element:
                    logger.info(f"Captcha detected on page via selector: {sel}")
                    self._report(f"CAPTCHA detected ({sel})")
                    return True
        except Exception as e:
            logger.debug(f"Captcha selector check error: {e}")

        logger.debug(f"No captcha found on: {page_url[:60]}")
        return False

    async def detect_and_solve_captcha(self, page, max_retries: int = 3,
                                        force_solve: bool = False) -> bool:
        """
        Detect captcha and handle it.

        - Google Sorry pages → auto-solve via 2Captcha (3 attempts, fresh data-s).
          On failure, mark Google blocked and return False (caller goes direct).
        - Regular CAPTCHA with force_solve=False → skip the page (do not spend solver).
        - Regular CAPTCHA with force_solve=True → auto-solve (must-solve path),
          except Google SERP chrome which is treated as a clean page.

        Returns True  — page is clean or CAPTCHA was cleared.
        Returns False — CAPTCHA blocks and could not be cleared.
        """
        try:
            page_url = page.url or ""
        except Exception:
            page_url = ""

        is_sorry = await self.is_google_sorry_block(page)
        if not is_sorry:
            if force_solve and self._google_recaptcha_chrome_is_normal(page_url):
                return True
            has_captcha = await self.detect_captcha(page)
            if not has_captcha:
                return True
            if not force_solve:
                logger.info(
                    "CAPTCHA detected — skipping page (solver reserved for must-solve paths)"
                )
                self._report(
                    "CAPTCHA detected — skipping this page, trying another route..."
                )
                return False

        solver_ok = self._captcha_solver and self._captcha_solver.is_configured
        if not solver_ok:
            logger.info("No CAPTCHA solver API key — cannot auto-solve")
            self._report("CAPTCHA blocking — no API key configured")
            if is_sorry:
                persist = CaptchaSolver.url_looks_like_sorry(page_url)
                self._mark_google_blocked(persist=persist)
            return False

        attempts = 3 if is_sorry else max(1, int(max_retries))
        kind = "Google Sorry" if is_sorry else "reCAPTCHA"
        logger.info(f"{kind} detected — attempting auto-solve ({attempts} tries)")
        self._report(f"{kind} — sending to solver ({attempts} attempts)...")

        proxy_config = await self._ensure_proxy_for_solve()

        for attempt in range(attempts):
            self._check_skip()
            self._report(f"CAPTCHA auto-solve {attempt + 1}/{attempts}...")

            if is_sorry and attempt > 0:
                await self._reload_sorry_for_fresh_datas(page)
                if not await self.is_google_sorry_block(page):
                    logger.info("Google Sorry cleared after reload")
                    self._report("CAPTCHA gone after reload — continuing")
                    return True

            solved = await self._captcha_solver.solve_recaptcha_on_page(
                page,
                activity_cb=self._activity_cb,
                proxy_config=proxy_config,
                profile_id=self._profile_id,
            )
            if solved:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

                if is_sorry:
                    if not await self.is_google_sorry_block(page):
                        logger.info("Google Sorry cleared by auto-solve")
                        self._report("CAPTCHA solved — continuing")
                        return True
                elif not await self.detect_captcha(page):
                    logger.info("CAPTCHA solved — page is clean")
                    self._report("CAPTCHA solved — continuing")
                    return True
                if await self.is_google_sorry_block(page):
                    logger.warning(
                        f"Auto-solve token rejected, still on Sorry "
                        f"(attempt {attempt + 1})"
                    )

            else:
                logger.warning(f"CAPTCHA solve attempt {attempt + 1} failed")

            if attempt < attempts - 1:
                self._report(f"CAPTCHA retry {attempt + 2}/{attempts} (fresh data-s)...")
                await asyncio.sleep(2)
                continue

        if is_sorry:
            try:
                final_url = page.url or page_url
            except Exception:
                final_url = page_url
            persist = CaptchaSolver.url_looks_like_sorry(final_url)
            self._mark_google_blocked(persist=persist)
            self._report(
                "CAPTCHA unsolved after 3 attempts — skipping Google, continuing via direct links"
            )
        else:
            self._report("CAPTCHA solve didn't clear — skipping page")
        return False

    # ══════════════════════════════════════════════════════════════
    #  CLOUDFLARE / BOT CHALLENGE DETECTION
    # ══════════════════════════════════════════════════════════════

    async def handle_cloudflare(self, page, max_wait: float = 15.0) -> bool:
        """
        Detect and wait for Cloudflare "checking your browser" challenges.
        AdsPower + stealth usually auto-resolves these.
        Returns True if challenge was resolved, False if stuck.
        """
        try:
            elapsed = 0.0
            check_interval = 2.0
            notified = False

            while elapsed < max_wait:
                title = await page.title()
                content = await page.content()
                title_lower = title.lower()
                content_lower = content[:3000].lower()

                is_cf_challenge = (
                    "just a moment" in title_lower
                    or "checking your browser" in content_lower
                    or "cf-browser-verification" in content_lower
                    or "challenge-platform" in content_lower
                    or "cloudflare" in title_lower and "ray id" in content_lower
                )

                if not is_cf_challenge:
                    return True  # Challenge resolved or wasn't there

                if not notified:
                    notified = True
                    try:
                        page_url = page.url
                    except Exception:
                        page_url = "unknown"
                    logger.info(f"Cloudflare challenge detected on {page_url[:60]}")
                    self._report("Cloudflare challenge detected — waiting for auto-resolve...")
                    if self._manual_captcha_cb:
                        try:
                            self._manual_captcha_cb("cloudflare", {"url": page_url})
                        except Exception:
                            pass

                logger.debug(f"Cloudflare challenge detected, waiting... ({elapsed:.0f}s)")
                await asyncio.sleep(check_interval)
                elapsed += check_interval

            logger.info("Cloudflare challenge did not resolve in time")
            return False
        except Exception as e:
            logger.debug(f"Cloudflare check error: {e}")
            return True  # Assume OK on error

    # ══════════════════════════════════════════════════════════════
    #  AUTH / LOGIN REDIRECT DETECTION
    # ══════════════════════════════════════════════════════════════

    async def check_auth_redirect(self, page, original_url: str) -> bool:
        """
        Check if navigation landed on a login/register page.
        Returns True if we got redirected to auth (bad).
        """
        if page.is_closed():
            return False
        try:
            current_url = page.url.lower()
            auth_indicators = [
                "/login", "/signin", "/sign-in", "/sign_in",
                "/signup", "/sign-up", "/sign_up", "/register",
                "/auth", "/oauth", "/sso",
                "/account/login", "/accounts/login",
                "returnurl=", "redirect_uri=", "next=",
            ]
            if any(indicator in current_url for indicator in auth_indicators):
                # Check that original URL didn't already have these
                if not any(indicator in original_url.lower() for indicator in auth_indicators):
                    logger.info(f"Auth redirect detected: {current_url[:80]}")
                    return True
        except Exception:
            pass
        return False

    # ══════════════════════════════════════════════════════════════
    #  DEAD PAGE / 404 DETECTION
    # ══════════════════════════════════════════════════════════════

    _DEAD_PAGE_TITLE_PATTERNS = (
        "404", "page not found", "not found", "doesn't exist",
        "does not exist", "no longer available", "page is gone",
        "we couldn't find", "we can't find", "nothing here",
        "oops", "sorry, this page", "error 404", "410 gone",
        "page removed", "this page isn't available",
        "page doesn't exist", "page does not exist",
        "страница не найдена", "ошибка 404",
        "seite nicht gefunden", "page introuvable",
        "página no encontrada", "pagina non trovata",
    )

    _DEAD_PAGE_BODY_PATTERNS = (
        "page not found", "page you requested was not found",
        "page you are looking for",
        "the page you were looking for doesn't exist",
        "this page doesn't exist", "this page does not exist",
        "we couldn't find the page", "we can't find the page",
        "couldn't find that page", "can't find that page",
        "sorry, we couldn't find", "sorry, we can't find",
        "no longer exists", "has been removed", "has been deleted",
        "nothing was found", "sorry, there is nothing at this address",
        "are you lost", "there's nothing here",
        "this page may have been moved or deleted",
        "content you're looking for can't be found",
        "404 error", "error 404", "http error 404", "http 404",
        "410 gone", "page has been removed",
        "we've been notified so we can fix",
        "were you looking for one of these",
        "the requested url was not found",
        "this is not the page you're looking for",
        "that page doesn't exist", "that page does not exist",
        "page you requested doesn't exist",
        "requested page could not be found",
        "seite nicht gefunden", "page introuvable",
        "página no encontrada", "pagina non trovata",
        "страница не найдена",
    )

    _DEAD_PAGE_SAFE_DOMAINS = (
        "google.com", "youtube.com", "bing.com", "yahoo.com",
        "duckduckgo.com",
    )

    async def is_dead_page(self, page, http_status: int = 0) -> bool:
        """Detect 404 / 'page not found' / dead pages.

        Checks HTTP status code and scans title + visible body text
        for common error patterns. Skips detection on major search
        engines and known-safe domains.
        Returns True if the page looks dead / non-existent.
        """
        if page.is_closed():
            return False

        try:
            current_url = page.url.lower()
            if any(d in current_url for d in self._DEAD_PAGE_SAFE_DOMAINS):
                return False
        except Exception:
            return False

        if http_status and http_status in (404, 410, 451):
            logger.info(f"Dead page (HTTP {http_status}): {current_url[:60]}")
            return True

        try:
            title = (await page.title()).lower()
            if any(p in title for p in self._DEAD_PAGE_TITLE_PATTERNS):
                logger.info(f"Dead page (title match): {title[:60]}")
                return True
        except Exception:
            pass

        try:
            body_text = await page.evaluate(
                "() => (document.body ? document.body.innerText : '').substring(0, 3000)"
            )
            if not body_text:
                return False
            body_lower = body_text.lower()

            if len(body_text.strip()) < 300:
                if any(p in body_lower for p in self._DEAD_PAGE_BODY_PATTERNS):
                    logger.info(f"Dead page (short body match): {current_url[:60]}")
                    return True
            else:
                # Scan first 1500 chars — nav menus can easily use 500+
                scan_area = body_lower[:1500]
                if any(p in scan_area for p in self._DEAD_PAGE_BODY_PATTERNS):
                    logger.info(f"Dead page (body match): {current_url[:60]}")
                    return True
        except Exception:
            pass

        return False

    # ══════════════════════════════════════════════════════════════
    #  BROWSER DIALOG HANDLING
    # ══════════════════════════════════════════════════════════════

    def setup_dialog_handler(self, page):
        """
        Set up automatic handling for browser-level dialogs.
        alert() → dismiss, confirm() → dismiss, prompt() → dismiss.
        Also handles beforeunload ("Leave site?") dialogs.
        """
        async def handle_dialog(dialog):
            try:
                logger.debug(f"Browser dialog: type={dialog.type}, message={dialog.message[:50]}")
                await dialog.dismiss()
            except Exception:
                pass

        page.on("dialog", handle_dialog)

    async def maybe_toggle_bookmarks_bar(self, page):
        """Randomly hide the bookmarks bar like a real user who tidied their browser.
        Uses Ctrl+Shift+B (Windows/Linux) — same as a human pressing it.
        Called once per session at startup with ~40% probability.
        """
        if random.random() > 0.40:
            return
        try:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await page.keyboard.press("Control+Shift+B")
            logger.debug("[Browser] Toggled bookmarks bar")
            await asyncio.sleep(random.uniform(0.3, 1.0))
        except Exception as e:
            logger.debug(f"[Browser] Bookmarks bar toggle failed: {e}")

    # ══════════════════════════════════════════════════════════════
    #  SAFE PAGE NAVIGATION (wraps everything)
    # ══════════════════════════════════════════════════════════════

    def _page_matches_url(self, page, url: str) -> bool:
        """True if the open tab is already on the requested host."""
        try:
            if page.is_closed():
                return False
            current = self._clean_search_host(page.url)
            wanted = self._clean_search_host(url)
        except Exception:
            return False
        if not current or not wanted:
            return False
        return (
            wanted == current
            or current.endswith("." + wanted)
            or wanted.endswith("." + current)
        )

    async def _page_has_body(self, page) -> bool:
        try:
            if page.is_closed():
                return False
            cur = (page.url or "").lower()
            if not cur or cur.startswith("about:blank") or cur.startswith("chrome://"):
                return False
            n = await page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText.length : 0"
            )
            return int(n or 0) > 20
        except Exception:
            return False

    async def safe_navigate(self, page, url: str, full_load: bool = True,
                            timeout_ms: int = None) -> bool:
        """
        Navigate to a URL with full protection:
        1. Load the page (wait for networkidle to allow JS/tracking/cookies)
        2. Handle Cloudflare challenges
        3. Dismiss popups/cookies
        4. Check for captcha → skip if found
        5. Check for auth redirect → go back if found
        Returns True if page is safe to interact with.
        """
        self._check_stop()  # Hard stop check
        timeout = timeout_ms if timeout_ms is not None else self.timing.get(
            "page_load_timeout_ms", 30000)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 30000
        timeout = max(10000, timeout)

        try:
            domain = urlparse(url).netloc or url[:40]
        except Exception:
            domain = url[:40]
        self._report(f"Opening {domain}")

        http_status = 0
        try:
            response = await page.goto(
                url, wait_until="commit", timeout=timeout)
            http_status = response.status if response else 0
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:
                pass
        except Exception as e:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            usable = self._page_matches_url(page, url) or await self._page_has_body(page)
            if usable:
                logger.info(
                    f"goto timed out but tab is usable ({(page.url or '')[:60]}) — continuing"
                )
            else:
                logger.debug(f"Navigation failed ({url[:50]}): {e}")
                if self._is_network_error(e):
                    self._fire_network_error(url, e)
                return False

        try:
            if full_load:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(timeout, 15000))
                except Exception:
                    pass

            await self._ensure_tab_visible(page)
            await asyncio.sleep(random.uniform(1, 3))

            cf_ok = await self.handle_cloudflare(page)
            if not cf_ok:
                logger.info(f"Skipping {url[:50]} — Cloudflare challenge stuck")
                return False

            await self.dismiss_popups(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            await self.dismiss_popups(page)

            if await self.is_dead_page(page, http_status):
                logger.info(f"Skipping {url[:50]} — dead page (HTTP {http_status})")
                self._report("Page not found — skipping")
                return False

            if not await self.detect_and_solve_captcha(page):
                logger.info(f"Skipping {url[:50]} — captcha detected")
                return False

            if await self.check_auth_redirect(page, url):
                logger.info(f"Skipping {url[:50]} — redirected to login")
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                return False

            if self._nav_success_cb:
                try:
                    self._nav_success_cb()
                except Exception:
                    pass
            return True

        except Exception as e:
            if self._page_matches_url(page, url):
                logger.info(
                    f"Post-load checks failed but tab is on {url[:50]} — treating as landed"
                )
                return True
            logger.debug(f"Navigation follow-up failed ({url[:50]}): {e}")
            if self._is_network_error(e):
                self._fire_network_error(url, e)
            return False

    # ══════════════════════════════════════════════════════════════
    #  SAFE LINK CLICKING (with tab protection)
    # ══════════════════════════════════════════════════════════════

    async def safe_click_link(self, page, context, visited_urls: set = None) -> bool:
        """
        Click a content link with full protection:
        - Track open tabs before/after to detect if tab was killed
        - Dismiss any popups that appear after clicking
        - Check for auth redirects after clicking
        - Check for captcha after clicking
        - Track visited URLs to avoid revisiting
        Returns True if click was successful and page is usable.
        """
        try:
            if page.is_closed():
                return False

            pages_before = len(context.pages)
            original_url = page.url

            clicked, href = await self._click_content_link(page, visited_urls)
            if not clicked:
                return False

            # Record visited URL
            if visited_urls is not None and href:
                try:
                    clean = href.split("?")[0].split("#")[0].rstrip("/")
                    visited_urls.add(clean)
                except Exception:
                    pass

            # Check if our tab got killed (RunPod-style popup close)
            await asyncio.sleep(0.5)
            if page.is_closed():
                logger.info("Tab was closed after click — recovering")
                return False

            # Check if a new tab opened instead (some links do target="_blank")
            pages_after = len(context.pages)
            if pages_after > pages_before:
                new_page = context.pages[-1]
                await self._ensure_tab_visible(new_page)
                await asyncio.sleep(1)
                await self.dismiss_popups(new_page)
                return True

            # Handle Cloudflare on new page
            cf_ok = await self.handle_cloudflare(page)
            if not cf_ok:
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                return False

            # Dismiss any popups that appeared after click
            await self.dismiss_popups(page)

            # Check for dead / 404 pages after click
            if await self.is_dead_page(page):
                logger.debug("Dead page after click — going back")
                self._report("Page not found — going back")
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                return False

            # Check for captcha — try to solve, go back if unsolvable
            if not await self.detect_and_solve_captcha(page):
                logger.debug("Captcha after click — going back")
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                return False

            # Check for auth redirect
            if await self.check_auth_redirect(page, original_url):
                logger.debug("Auth redirect after click — going back")
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                return False

            # Also record the actual landed URL (might differ from href due to redirects)
            if visited_urls is not None and not page.is_closed():
                try:
                    landed = page.url.split("?")[0].split("#")[0].rstrip("/")
                    visited_urls.add(landed)
                except Exception:
                    pass

            return True

        except Exception as e:
            logger.debug(f"Safe click error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    #  CORE HELPERS
    # ══════════════════════════════════════════════════════════════

    def _fatigue_factor(self) -> float:
        """Return a delay multiplier that grows as the session wears on.

        Early in a session a user is quick and focused (~0.9x); by the end they
        linger and drift (~1.6x). This makes each profile's pacing drift over
        time instead of being statistically flat (a bot tell).
        """
        try:
            frac = (time.monotonic() - self._session_start) / self._session_span_s
        except Exception:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        return 0.9 + 0.7 * frac

    def _log_normal_delay(self, min_s: float, max_s: float) -> float:
        """Sample a long-tailed (log-normal-ish) delay within [min_s, max_s].

        Humans mostly pause briefly but occasionally stall for much longer;
        uniform randoms lack that tail and are statistically detectable.
        """
        if max_s <= min_s:
            return min_s
        span = max_s - min_s
        # mu/sigma chosen so the bulk sits in the lower third with a long tail.
        sample = random.lognormvariate(-0.4, 0.7)  # median ~0.67, tail > 2
        val = min_s + min(sample, 3.0) / 3.0 * span
        return max(min_s, min(val, max_s))

    async def random_sleep(self, min_s: float = None, max_s: float = None):
        """Sleep for a random duration with human-like distribution."""
        self._check_skip()
        if min_s is None:
            min_s = self.timing.get("action_delay_min", 2)
        if max_s is None:
            max_s = self.timing.get("action_delay_max", 12)

        # Long-tailed base delay, then scaled by session fatigue.
        base = self._log_normal_delay(min_s, max_s) * self._fatigue_factor()

        # Cap at 30s absolute max — no human waits 90 seconds staring
        base = min(base, 30.0)

        # Split long sleeps into chunks so skip/pause/stop can interrupt
        elapsed = 0.0
        while elapsed < base:
            self._check_skip()  # Also checks stop
            if self._pause_check:
                await self._pause_check()
            chunk = min(0.5, base - elapsed)  # Check every 0.5s for fast hard stop
            await asyncio.sleep(chunk)
            elapsed += chunk


    async def thinking_pause(self):
        """Simulate the pause when a human is thinking/deciding (1-4 seconds)."""
        self._check_stop()
        await asyncio.sleep(random.uniform(1.0, 4.0))


    # ══════════════════════════════════════════════════════════════
    #  SCROLLING — ultra-realistic
    # ══════════════════════════════════════════════════════════════

    async def scroll_page(self, page):
        """
        Scroll the page like a real person — fast, natural, not robotic.
        Uses mouse wheel for realism (not window.scrollBy which looks automated).
        """
        try:
            min_px = int(self.timing.get("scroll_min_px", 300) or 300)
            max_px = int(self.timing.get("scroll_max_px", 900) or 900)
            if min_px > max_px:
                min_px, max_px = max_px, min_px
            min_px = max(1, min_px)
            max_px = max(min_px, max_px)

            pattern = random.choice([
                "steady_reader",    # Normal reading scroll
                "skimmer",          # Fast scroll-through
                "explorer",         # Mixed up/down
                "bottom_checker",   # Quick check bottom, come back
            ])

            if pattern == "steady_reader":
                num_scrolls = random.randint(3, 7)
                for i in range(num_scrolls):
                    distance = random.randint(min_px, int(max_px * 0.7))
                    if random.random() < 0.12 and i > 1:
                        distance = -random.randint(100, 300)
                    await page.mouse.wheel(0, distance)
                    await asyncio.sleep(random.uniform(0.6, 2.5))

            elif pattern == "skimmer":
                num_scrolls = random.randint(4, 8)
                for _ in range(num_scrolls):
                    distance = random.randint(int(max_px * 0.5), max_px)
                    await page.mouse.wheel(0, distance)
                    if random.random() < 0.25:
                        await asyncio.sleep(random.uniform(1.0, 3.0))
                    else:
                        await asyncio.sleep(random.uniform(0.2, 0.8))

            elif pattern == "explorer":
                num_scrolls = random.randint(4, 8)
                for i in range(num_scrolls):
                    if random.random() < 0.25 and i > 0:
                        distance = -random.randint(150, int(max_px * 0.5))
                    else:
                        distance = random.randint(min_px, max_px)
                    await page.mouse.wheel(0, distance)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

            elif pattern == "bottom_checker":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.7)")
                await asyncio.sleep(random.uniform(0.8, 2.0))
                scroll_back = random.uniform(0.1, 0.4)
                await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {scroll_back})")
                await asyncio.sleep(random.uniform(1.0, 2.5))
                for _ in range(random.randint(2, 4)):
                    distance = random.randint(min_px, int(max_px * 0.6))
                    await page.mouse.wheel(0, distance)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

        except Exception as e:
            logger.debug(f"Scroll error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  MOUSE MOVEMENT — realistic curves and fidgeting
    # ══════════════════════════════════════════════════════════════

    async def move_mouse_randomly(self, page):
        """
        Move the mouse like a real person:
        - Curved, not straight-line movements
        - Variable speed (fast across empty space, slow near targets)
        - Occasional fidgeting / small jitter
        - Sometimes parks mouse at edge of screen
        """
        try:
            viewport = page.viewport_size
            if not viewport:
                return

            w, h = viewport["width"], viewport["height"]
            num_moves = random.randint(2, 7)

            for i in range(num_moves):
                behavior = random.choice(["wander", "target", "fidget", "park"])

                if behavior == "wander":
                    x = random.randint(50, max(51, w - 50))
                    y = random.randint(50, max(51, h - 50))
                    steps = random.randint(8, 30)
                    await page.mouse.move(x, y, steps=steps)
                    await asyncio.sleep(random.uniform(0.2, 1.0))

                elif behavior == "target":
                    # Move toward content area (center-ish)
                    x = random.randint(int(w * 0.15), int(w * 0.85))
                    y = random.randint(int(h * 0.2), int(h * 0.7))
                    steps = random.randint(10, 25)
                    await page.mouse.move(x, y, steps=steps)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

                elif behavior == "fidget":
                    # Small jittery movements around current position
                    for _ in range(random.randint(2, 5)):
                        dx = random.randint(-30, 30)
                        dy = random.randint(-20, 20)
                        x = max(5, min(w - 5, int(w * 0.5) + dx))
                        y = max(5, min(h - 5, int(h * 0.5) + dy))
                        await page.mouse.move(x, y, steps=random.randint(2, 6))
                        await asyncio.sleep(random.uniform(0.05, 0.3))

                elif behavior == "park":
                    # Move mouse to edge (user looking away)
                    side = random.choice(["top", "right", "bottom"])
                    if side == "top":
                        x, y = random.randint(100, w - 100), random.randint(5, 30)
                    elif side == "right":
                        x, y = random.randint(w - 50, w - 5), random.randint(100, h - 100)
                    else:
                        x, y = random.randint(100, w - 100), random.randint(h - 50, h - 5)
                    await page.mouse.move(x, y, steps=random.randint(10, 20))
                    await asyncio.sleep(random.uniform(1.0, 4.0))

        except Exception as e:
            logger.debug(f"Mouse move error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  TYPING — with typos, corrections, variable rhythm
    # ══════════════════════════════════════════════════════════════

    async def type_like_human(self, page, selector: str, text: str):
        """
        Type text like a real person:
        - Variable speed per character with realistic 80-300ms per key
        - Occasional typos followed by backspace correction
        - Pauses between words (thinking about next word)
        - Bursts of fast typing then slowing down
        - Sometimes pauses mid-word (distraction / thinking)
        - Longer pauses at punctuation (end of thought)
        """
        try:
            element = await page.query_selector(selector)
            if not element:
                return False
            await element.click()
            await asyncio.sleep(random.uniform(0.6, 2.5))  # Settling in, looking at field

            delay_min = self.timing.get("typing_delay_min_ms", 80)
            delay_max = self.timing.get("typing_delay_max_ms", 280)

            # Choose a typing personality for this session
            # 0.8-1.4 range: most people are average, few are notably fast/slow
            speed_factor = random.triangular(0.8, 1.4, 1.0)

            # Occasional rhythm shifts mid-typing (fatigue, distraction)
            rhythm_shift_at = random.randint(len(text) // 3, max(len(text) - 2, 1))

            for i, char in enumerate(text):
                # Shift rhythm partway through (simulates fatigue or refocus)
                if i == rhythm_shift_at:
                    speed_factor *= random.uniform(0.85, 1.25)

                # Variable delay per character
                base_delay = random.uniform(delay_min, delay_max) * speed_factor

                # Faster for common letter sequences (muscle memory)
                if char in "etaoinsrhld":
                    base_delay *= 0.75
                # Slower for numbers, symbols (need to look at keyboard)
                elif char.isdigit() or char in "!@#$%^&*()":
                    base_delay *= 1.6
                # Slower for uppercase (shift key coordination)
                elif char.isupper():
                    base_delay *= 1.3

                # 5% chance of typo — type wrong char, notice, backspace, retype
                if random.random() < 0.05 and char.isalpha():
                    wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz")
                    await page.keyboard.type(wrong_char, delay=base_delay)
                    await asyncio.sleep(random.uniform(0.3, 1.2))  # Noticing the typo
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.15, 0.5))

                await page.keyboard.type(char, delay=base_delay)

                # Pause between words — people think about the next word
                if char == " ":
                    if random.random() < 0.45:
                        await asyncio.sleep(random.uniform(0.4, 2.0))

                # Pause after punctuation (end of thought / clause)
                elif char in ".!?,;:":
                    if random.random() < 0.6:
                        await asyncio.sleep(random.uniform(0.3, 1.5))

                # Occasional mid-word pause (thinking, distraction)
                elif random.random() < 0.07:
                    await asyncio.sleep(random.uniform(0.4, 2.5))

            return True
        except Exception as e:
            logger.debug(f"Typing error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    #  GOOGLE SESSION WARMUP — build Google cookies/history first
    # ══════════════════════════════════════════════════════════════

    async def warmup_google_session(self, page, context=None, force: bool = False):
        """
        Build a realistic Google session before searching.
        Real users don't just land on Google.com and search — they browse Google services first.
        
        This function:
        1. Visits Google homepage (builds cookies)
        2. Randomly browses Google services (Maps, Images, News, YouTube)
        3. Returns to homepage
        4. Adds realistic delays between actions
        
        Call this before the first search of a session to avoid detection.
        """
        try:
            # Check if we've already warmed up Google in this session (unless forced)
            if not force and hasattr(self, '_google_warmed_up'):
                return True
            
            self._report("Warming up Google session...")
            
            # Step 1: Visit Google homepage naturally (not always direct)
            entry_method = random.choice(["direct", "referrer", "bookmark"])
            
            if entry_method == "referrer" and context:
                # Come from another site first (more natural)
                referrer_sites = [
                    "https://www.wikipedia.org",
                    "https://www.reddit.com",
                    "https://www.github.com",
                ]
                ref_site = random.choice(referrer_sites)
                await self.safe_navigate(page, ref_site)
                await asyncio.sleep(random.uniform(3, 8))
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(2, 5))
            
            # Navigate to Google homepage
            ok = await self.safe_navigate(page, "https://www.google.com")
            if not ok:
                return False
            
            # Dwell on homepage — look around, move mouse
            await asyncio.sleep(random.uniform(2, 5))
            await self.move_mouse_randomly(page)
            await asyncio.sleep(random.uniform(1, 3))
            
            # Step 2: Browse Google services (30-50% chance)
            if random.random() < 0.4:
                google_services = [
                    ("https://www.google.com/maps", "Google Maps"),
                    ("https://www.google.com/images", "Google Images"),
                    ("https://news.google.com", "Google News"),
                    ("https://www.google.com/trends", "Google Trends"),
                ]
                service_url, service_name = random.choice(google_services)
                self._report(f"Browsing {service_name}...")
                
                try:
                    await self.safe_navigate(page, service_url)
                    await asyncio.sleep(random.uniform(3, 8))
                    await self.scroll_page(page)
                    await asyncio.sleep(random.uniform(2, 4))
                    await self.move_mouse_randomly(page)
                    
                    # Sometimes interact with the service
                    if random.random() < 0.3:
                        await self.simulate_reading(page)
                    
                    # Return to Google homepage
                    await self.safe_navigate(page, "https://www.google.com")
                    await asyncio.sleep(random.uniform(1, 3))
                except Exception:
                    pass  # Continue even if service visit fails
            
            # Mark as warmed up
            self._google_warmed_up = True
            return True
            
        except Exception as e:
            logger.debug(f"Google warmup error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    #  GOOGLE SEARCH — full realistic flow
    # ══════════════════════════════════════════════════════════════

    async def simulate_google_search(self, page, query: str,
                                     target_domains: list = None):
        """
        Full realistic Google search with optional click-through to target domains.

        If target_domains is provided, tries to find and click a result matching
        one of those domains (building a search→site referrer chain).
        Otherwise clicks a random result.
        """
        try:
            import time as _time

            # ── Google only. If blocked, skip this search. ──
            if self._google_blocked or not self._can_use_google():
                self._report("Skipping Google search (budget/blocked)")
                return False
            await self._await_search_slot()
            self._google_search_count += 1

            # Enforce minimum delay between searches (avoid rapid-fire detection)
            current_time = _time.time()
            time_since_last_search = current_time - self._last_google_search_time
            min_delay_between_searches = random.uniform(15, 45)  # 15-45 seconds minimum
            
            if self._last_google_search_time > 0 and time_since_last_search < min_delay_between_searches:
                wait_time = min_delay_between_searches - time_since_last_search
                self._report(f"Waiting {int(wait_time)}s before next search (natural pacing)...")
                await asyncio.sleep(wait_time)
            
            # Warm up Google session before first search (builds cookies/history)
            if not self._google_warmed_up:
                await self.warmup_google_session(page)
                # Add extra delay after warmup before searching
                await asyncio.sleep(random.uniform(3, 8))
            
            self._report(f"Google search: \"{query[:35]}\"")
            
            # Don't always navigate directly to Google — sometimes we're already there
            current_url = page.url.lower()
            is_already_on_google = "google.com" in current_url
            
            if not is_already_on_google:
                # 20% chance to come from another site first (more natural entry)
                if random.random() < 0.2:
                    referrer_sites = [
                        "https://www.wikipedia.org",
                        "https://www.reddit.com",
                    ]
                    ref = random.choice(referrer_sites)
                    await self.safe_navigate(page, ref)
                    await asyncio.sleep(random.uniform(2, 5))
                    await self.scroll_page(page)
                    await asyncio.sleep(random.uniform(1, 3))
                
                ok = await self.safe_navigate(page, "https://www.google.com")
                if not ok:
                    return
            else:
                # Already on Google — just wait a bit (like user is thinking)
                await asyncio.sleep(random.uniform(2, 5))
            
            # Update last search time
            self._last_google_search_time = _time.time()

            # Random delay before starting to type (looking at the page)
            # Longer delays = more human-like (users don't immediately start typing)
            await asyncio.sleep(random.uniform(2.0, 7.0))

            # Move mouse toward search bar before clicking (more natural)
            await self.move_mouse_randomly(page)
            await asyncio.sleep(random.uniform(0.5, 2.0))

            for selector in ['textarea[name="q"]', 'input[name="q"]']:
                success = await self.type_like_human(page, selector, query)
                if success:
                    break
            else:
                await self.safe_navigate(
                    page,
                    f"https://www.google.com/search?q={query.replace(' ', '+')}",
                )
                await self.random_sleep(2, 5)
                return

            # Pause after typing — looking at autocomplete suggestions
            # Longer pause = more human (users read suggestions)
            await asyncio.sleep(random.uniform(1.5, 4.5))

            # 15% chance: clear and retype (changed mind about query) - more human
            if random.random() < 0.15:
                await page.keyboard.down("Control")
                await page.keyboard.press("a")
                await page.keyboard.up("Control")
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.5, 1.5))
                modified = query + " " + random.choice(["review", str(datetime.now().year), "guide", "tutorial", "tips"])
                for selector in ['textarea[name="q"]', 'input[name="q"]']:
                    success = await self.type_like_human(page, selector, modified)
                    if success:
                        break
                await asyncio.sleep(random.uniform(0.5, 1.5))

            await page.keyboard.press("Enter")

            # Wait for results with variable patience (longer = more human)
            await asyncio.sleep(random.uniform(2.0, 6.0))

            # Dismiss any popups on search results
            await self.dismiss_popups(page)

            # Check for Google sorry/CAPTCHA page
            # force_solve=True: no alternative route from Google SERP
            if not await self.detect_and_solve_captcha(page, force_solve=True):
                self._report("Google search blocked — continuing warmup via direct links")
                self._google_warmed_up = False
                return False

            # Natural SERP behavior: scroll slowly, move mouse, scan results
            # Don't immediately click — real users scan results first
            await self.scroll_page(page)
            await asyncio.sleep(random.uniform(2, 5))  # Longer pause = more realistic
            await self.move_mouse_randomly(page)
            await asyncio.sleep(random.uniform(1, 3))
            
            # Sometimes scroll more before clicking (scanning more results)
            if random.random() < 0.4:
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(1, 3))

            if target_domains:
                clicked = await self._click_targeted_search_result(page, target_domains)
                if not clicked:
                    # Fallback: click any result
                    await self._click_search_result(page)
            elif random.random() < 0.7:
                await self._click_search_result(page)

        except Exception as e:
            logger.debug(f"Google search error: {e}")

    async def _collect_search_results(self, page) -> list:
        """Collect all visible Google search result links with their hrefs."""
        results = []
        result_selectors = [
            "h3",
            "[data-header-feature] a",
            "a h3",
        ]
        for sel in result_selectors:
            elements = await page.query_selector_all(sel)
            for el in elements[:15]:
                try:
                    if not await el.is_visible():
                        continue
                    handle = await el.evaluate_handle(
                        "el => el.closest('a') || el.parentElement?.closest('a')"
                    )
                    parent_a = handle.as_element() if handle else None
                    if parent_a:
                        href = await parent_a.get_attribute("href")
                        if (
                            href
                            and href.startswith("http")
                            and "google.com" not in href
                            and not self._is_blocked_link(href, "")
                        ):
                            results.append((el, href))
                except Exception:
                    continue
            if results:
                break
        return results

    async def _click_targeted_search_result(self, page, target_domains: list) -> bool:
        """
        Click a Google result that matches one of the target domains.
        Builds search→site referrer chain (critical for warmup).
        Returns True if a targeted result was found and clicked.
        """
        try:
            results = await self._collect_search_results(page)
            if not results:
                return False

            # Look for a result matching a target domain
            for el, href in results:
                href_lower = href.lower()
                for domain in target_domains:
                    domain_clean = domain.replace("https://", "").replace("http://", "").split("/")[0].lower()
                    if domain_clean in href_lower:
                        # Scroll result into view, hover, then click
                        try:
                            await el.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                        await el.hover()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await el.click()
                        await self.random_sleep(3, 8)
                        await self.dismiss_popups(page)
                        await self.simulate_reading(page)
                        logger.debug(f"Targeted click-through to {domain_clean}")
                        return True

            return False
        except Exception as e:
            logger.debug(f"Targeted search result click error: {e}")
            return False

    async def _click_search_result(self, page):
        """Click a random Google search result (the blue links)."""
        try:
            results = await self._collect_search_results(page)
            if not results:
                return

            # Pick one of the top results (like a real user scans top 3-5)
            el, href = random.choice(results[:min(3, len(results))])
            await el.hover()
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await el.click()
            await self.random_sleep(3, 8)

            # Dismiss popups on the result page
            await self.dismiss_popups(page)

            # Read the page we landed on
            await self.simulate_reading(page)

            # 50% chance: go back to search results
            if random.random() < 0.5:
                await page.go_back()
                await asyncio.sleep(random.uniform(2, 4))
                await self.dismiss_popups(page)

        except Exception as e:
            logger.debug(f"Search result click error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  SEARCH-FIRST NAVIGATION (Google → click target site)
    # ══════════════════════════════════════════════════════════════

    # Map of well-known domains → multiple natural search query templates.
    # Each domain has varied phrasing so the same site is never searched identically.
    _DOMAIN_QUERY_MAP = {
        "openrouter": [
            "openrouter ai", "openrouter api", "openrouter models", "openrouter pricing",
            "openrouter ai chat", "openrouter llm api", "openrouter alternative",
            "best ai api router", "openrouter free models", "openrouter official site",
            "openrouter api platform", "openrouter website", "openrouter ai models list",
        ],
        "runpod": [
            "runpod gpu", "runpod serverless", "runpod pricing", "runpod deploy ai",
            "runpod cloud gpu rental", "runpod vs vast ai", "cheap gpu cloud runpod",
            "runpod official site", "runpod serverless gpu", "rent gpu runpod",
        ],
        "reddit": [
            "reddit", "reddit discussion", "reddit forum", "reddit community",
            "reddit front page", "reddit popular", "reddit website",
        ],
        "youtube": [
            "youtube", "youtube videos", "youtube watch", "youtube website",
            "watch videos youtube", "youtube official",
        ],
        "amazon": [
            "amazon", "amazon shopping", "buy on amazon", "amazon deals",
            "amazon official site", "amazon online store", "shop amazon",
        ],
        "ebay": [
            "ebay", "ebay buy", "ebay deals", "ebay auction",
            "ebay official site", "ebay online shopping",
        ],
        "github": [
            "github", "github open source", "github projects", "github repository",
            "github official", "github code hosting", "github website",
        ],
        "stackoverflow": [
            "stackoverflow help", "stackoverflow programming", "stack overflow answers",
            "stack overflow questions", "stackoverflow official",
        ],
        "medium": ["medium articles", "medium blog", "medium read", "medium website"],
        "twitter": ["twitter", "x social media", "twitter posts", "x.com", "twitter official"],
        "linkedin": [
            "linkedin", "linkedin professional", "linkedin jobs", "linkedin website",
            "linkedin official site", "linkedin networking",
        ],
        "netflix": ["netflix", "netflix shows", "netflix movies", "netflix streaming", "netflix official"],
        "twitch": ["twitch", "twitch streams", "twitch live", "twitch official"],
        "spotify": ["spotify", "spotify music", "spotify playlists", "spotify official"],
        "walmart": ["walmart", "walmart shopping", "walmart deals", "walmart online"],
        "bestbuy": ["best buy", "best buy electronics", "best buy deals", "best buy official"],
        "target": ["target store", "target shopping", "target deals", "target official website"],
        "newegg": ["newegg", "newegg pc parts", "newegg deals", "newegg electronics"],
        "etsy": ["etsy", "etsy handmade", "etsy gifts", "etsy shop", "etsy official"],
        "aliexpress": ["aliexpress", "aliexpress deals", "aliexpress shopping", "aliexpress official"],
        "wish": ["wish shopping", "wish app deals", "wish official"],
        "nike": ["nike", "nike shoes", "nike store", "nike official", "nike website"],
        "adidas": ["adidas", "adidas shoes", "adidas store", "adidas official"],
        "hm": ["h&m", "h&m clothing", "h&m fashion", "h&m official"],
        "zara": ["zara", "zara clothing", "zara fashion", "zara official"],
        "imdb": ["imdb", "imdb movies", "imdb ratings", "imdb official"],
        "rottentomatoes": ["rotten tomatoes", "rotten tomatoes movies", "rotten tomatoes reviews"],
        "espn": ["espn", "espn sports", "espn scores", "espn official"],
        "cnn": ["cnn news", "cnn latest", "cnn official", "cnn breaking news"],
        "bbc": ["bbc news", "bbc world", "bbc official", "bbc website"],
        "nytimes": ["new york times", "nytimes articles", "nytimes official", "ny times website"],
        "wikipedia": ["wikipedia", "wiki", "wikipedia encyclopedia", "wikipedia official"],
        "quora": ["quora", "quora answers", "quora questions", "quora official"],
        "pinterest": ["pinterest", "pinterest ideas", "pinterest images", "pinterest official"],
        "tumblr": ["tumblr", "tumblr blog", "tumblr official"],
        "discord": ["discord", "discord server", "discord chat", "discord official", "discord app"],
        "telegram": ["telegram", "telegram messenger", "telegram official", "telegram app"],
        "whatsapp": ["whatsapp web", "whatsapp messenger", "whatsapp official"],
        "notion": ["notion", "notion app", "notion workspace", "notion official"],
        "figma": ["figma", "figma design", "figma tool", "figma official"],
        "canva": ["canva", "canva design", "canva templates", "canva official"],
        "adobe": ["adobe", "adobe creative cloud", "adobe official"],
        "microsoft": ["microsoft", "microsoft office", "microsoft official"],
        "apple": ["apple", "apple store", "apple products", "apple official"],
        "google": ["google", "google search", "google official"],
        "coinbase": ["coinbase", "coinbase crypto", "coinbase official", "coinbase exchange"],
        "binance": ["binance", "binance crypto trading", "binance exchange", "binance official"],
        "draftkings": ["draftkings", "draftkings betting", "draftkings official"],
        "fanduel": ["fanduel", "fanduel sportsbook", "fanduel official"],
        "bet365": ["bet365", "bet365 betting", "bet365 official"],
        "pokerstars": ["pokerstars", "pokerstars poker", "pokerstars official"],
        "steam": ["steam", "steam games", "steam store", "steam official", "steam game store"],
        "epicgames": ["epic games", "epic games store", "epic games official"],
        "gog": ["gog games", "gog store", "gog official"],
    }

    # Generic query patterns when no domain-specific entry exists.
    # Never include a full URL or path — real people type a site/brand name.
    _GENERIC_QUERY_PATTERNS = [
        "{name}",
        "{name} official",
        "{name} website",
        "{name} review",
        "{name} {year}",
        "what is {name}",
        "{name} site",
    ]

    _SKIP_HOST_LABELS = {
        "www", "com", "net", "org", "io", "gg", "tm", "store", "de", "co",
        "uk", "us", "wiki", "m", "en", "www2", "app", "shop",
    }

    _BRAND_QUERY_MAP = {
        "teamfortress": ["tf2 wiki", "team fortress 2 wiki", "tf2 item wiki"],
        "steamcommunity": ["steam community market", "steam market tf2"],
        "steampowered": ["steam store", "steam official"],
        "backpack": ["backpack.tf", "tf2 backpack.tf prices"],
        "marketplace": ["marketplace.tf", "tf2 marketplace.tf"],
        "mannco": ["mannco store", "mannco.store tf2", "buy tf2 items mannco"],
        "tf2": ["tf2.tm", "tf2 marketplace", "tf2.tm skins"],
        "scrap": ["scrap.tf", "scrap.tf tf2"],
        "stntrading": ["stn trading", "stntrading.eu tf2"],
        "rustskins": ["rustskins.gg", "rust skins rustskins"],
        "rustclash": ["rustclash", "rustclash skins"],
        "csfloat": ["csfloat", "csfloat market", "cs2 csfloat"],
        "skinport": ["skinport", "skinport rust skins", "skinport cs2"],
        "dmarket": ["dmarket", "dmarket skins", "dmarket cs2"],
        "bitskins": ["bitskins", "bitskins cs2"],
        "csmoney": ["cs.money", "cs.money trade"],
        "waxpeer": ["waxpeer", "waxpeer cs2"],
        "gamerpay": ["gamerpay", "gamerpay cs2"],
        "shadowpay": ["shadowpay", "shadowpay skins"],
        "csgoroll": ["csgoroll", "csgoroll skins"],
        "csgoempire": ["csgoempire", "csgoempire skins"],
        "eneba": ["eneba", "eneba keys", "eneba gift cards"],
        "kinguin": ["kinguin", "kinguin keys"],
        "cdkeys": ["cdkeys", "cdkeys steam"],
        "g2a": ["g2a", "g2a keys"],
        "g2g": ["g2g", "g2g marketplace"],
        "u7buy": ["u7buy", "u7buy gift cards"],
        "playerauctions": ["playerauctions", "playerauctions accounts"],
        "eldorado": ["eldorado.gg", "eldorado accounts"],
        "openrouter": ["openrouter", "openrouter ai", "openrouter models"],
        "anthropic": ["anthropic", "anthropic claude", "claude api"],
        "claude": ["claude ai", "claude.ai"],
        "cursor": ["cursor ide", "cursor.com"],
        "openai": ["openai", "openai api"],
        "chatgpt": ["chatgpt", "chat gpt"],
        "huggingface": ["huggingface", "hugging face models"],
        "reddit": ["reddit", "reddit homepage"],
        "youtube": ["youtube", "youtube videos"],
        "google": ["google", "google search"],
    }

    @staticmethod
    def _clean_search_host(url: str) -> str:
        """Return hostname only (no scheme, path, or www)."""
        raw = (url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        try:
            parsed = urlparse(raw)
            host = (parsed.netloc or parsed.path.split("/")[0] or "").lower()
        except Exception:
            host = raw.lower().split("/")[0]
        host = host.replace("www.", "").split(":")[0].split("?")[0].strip(".")
        return host

    @classmethod
    def _url_to_search_query(cls, url: str) -> str:
        """Natural Google query from a URL — brand/site name, never a path."""
        host = cls._clean_search_host(url)
        if not host:
            return "google"

        for key, queries in cls._BRAND_QUERY_MAP.items():
            if key in host:
                return random.choice(queries)

        for key, queries in cls._DOMAIN_QUERY_MAP.items():
            if key in host.split(".")[0] or key == host.split(".")[0]:
                return random.choice(queries)

        labels = [p for p in host.split(".") if p and p not in cls._SKIP_HOST_LABELS]
        name = (labels[-1] if labels else host.split(".")[0]).replace("-", " ")
        pattern = random.choice(cls._GENERIC_QUERY_PATTERNS)
        return pattern.format(name=name, year=datetime.now().year)

    async def _wait_full_page_load(self, page, timeout_ms: int = 20000):
        """
        Wait for full page load including JS, images, and tracking scripts.
        Uses 'networkidle' (no network requests for 500ms) so that tracking
        pixels, analytics JS, and cookie-setting scripts have time to fire.
        Falls back to 'load' if networkidle times out.
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            try:
                await page.wait_for_load_state("load", timeout=8000)
            except Exception:
                pass
        # Extra settle time for deferred JS / tracking beacons
        await asyncio.sleep(random.uniform(1.5, 3.5))

    async def _realistic_dwell(self, page, min_s: float = 2, max_s: float = 5):
        """
        Stay on page for a realistic duration, performing natural micro-actions.
        Ensures cookies from JS/tracking scripts are fully generated.
        Uses wall-clock time so scroll/mouse actions count toward the total.

        If timing config has dwell_min_s / dwell_max_s, those cap the dwell.
        """
        cfg_min = self.timing.get("dwell_min_s")
        cfg_max = self.timing.get("dwell_max_s")
        if cfg_min is not None:
            min_s = min(min_s, cfg_min)
        if cfg_max is not None:
            max_s = min(max_s, cfg_max)
        if min_s > max_s:
            min_s = max_s

        # Content-aware: longer pages get read longer (up to a cap), and the
        # whole dwell drifts up with session fatigue.
        content_bonus = 0.0
        try:
            text_len = await page.evaluate(
                "(document.body && document.body.innerText) ? document.body.innerText.length : 0"
            )
            # Short extra glance on long pages — do not sit still for cookies.
            content_bonus = min(float(text_len) / 2500.0, 1.5)
        except Exception:
            content_bonus = 0.0

        dwell_time = (random.uniform(min_s, max_s) + content_bonus) * self._fatigue_factor()
        dwell_time = min(dwell_time, max_s)  # never exceed the cap
        start = time.monotonic()

        while (time.monotonic() - start) < dwell_time:
            if page.is_closed():
                break

            action = random.choices(
                ["scroll", "mouse", "hover", "pause"],
                weights=[40, 25, 20, 15],
            )[0]
            try:
                if action == "scroll":
                    await self.scroll_page(page)
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                elif action == "mouse":
                    await self.move_mouse_randomly(page)
                    await asyncio.sleep(random.uniform(0.3, 1.2))
                elif action == "hover":
                    await self.hover_random_element(page)
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                else:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
            except Exception:
                await asyncio.sleep(0.5)

    # ══════════════════════════════════════════════════════════════
    #  ANTI-DETECTION: search-engine exposure control + rotation
    # ══════════════════════════════════════════════════════════════

    def set_target_host(self, url: str):
        """Remember the marketplace this session must reach via Google click-through."""
        self._target_host = self._clean_search_host(url)

    def _is_target_host(self, host_or_url: str) -> bool:
        target = getattr(self, "_target_host", "") or ""
        if not target or not host_or_url:
            return False
        host = self._clean_search_host(host_or_url)
        if not host:
            host = str(host_or_url).lower().replace("www.", "").split("/")[0].split(":")[0]
        return host == target or host.endswith("." + target) or target.endswith("." + host)

    def _can_use_google(self, for_host: str = None) -> bool:
        """Google is allowed if not blocked. Session budget applies to ambient sites only.

        The target marketplace always gets Google (budget does not skip it).
        A sorry-page block still refuses Google so the caller can wait/retry or skip.
        """
        if self._google_blocked:
            return False
        if for_host and self._is_target_host(for_host):
            return True
        return self._google_search_count < self._google_budget

    def _mark_google_blocked(self, persist: bool = True):
        """Flag Google as off-limits for the rest of the session.

        persist=True writes google_blocked_at (6h skip). Only use after a real
        /sorry/ URL that could not be cleared.
        """
        if not self._google_blocked:
            self._google_blocked = True
            self._report("Google flagged as blocked — continuing via direct links")
        if persist and self._on_google_blocked:
            try:
                self._on_google_blocked()
            except Exception:
                pass

    async def _await_search_slot(self):
        """Respect the cross-profile search stagger, if configured."""
        if self._search_gate:
            try:
                await self._search_gate.wait_for_slot()
            except Exception:
                pass

    async def _direct_warmup_visit(self, page, url: str, context=None,
                                   depth_override: tuple = None) -> bool:
        """Open a warmup URL directly when Google is blocked."""
        try:
            host = urlparse(url).netloc.replace("www.", "") or url[:40]
        except Exception:
            host = url[:40]
        self._report(f"Opening {host} directly (Google skipped)")
        logger.info(f"Direct warmup visit: {url[:80]}")
        try:
            await self.browse_site(page, url, context=context, depth_override=depth_override)
            if page.is_closed():
                return False
            landed = self._clean_search_host(page.url)
            wanted = self._clean_search_host(url)
            if wanted and landed:
                return (
                    wanted in landed
                    or landed.endswith(wanted)
                    or wanted.endswith(landed)
                    or wanted.split(".")[0] in landed
                )
            return True
        except Exception as e:
            logger.debug(f"Direct visit error: {e}")
            return False

    async def organic_arrival(self, page, context, url: str, query: str,
                               first_visit: bool = False,
                               allow_referrer: bool = True) -> bool:
        """Reach `url` via Google click-through, or directly if Google is blocked.

        `allow_referrer` is ignored — kept for call-site compatibility.
        """
        _ = allow_referrer
        if query and ("/" in query or "http" in query.lower()):
            query = self._url_to_search_query(url)

        host = self._clean_search_host(url)
        is_target = self._is_target_host(url)
        retries = 2 if (first_visit or is_target) else 0

        if self._google_blocked:
            return await self._direct_warmup_visit(page, url, context=context)

        for attempt in range(retries + 1):
            if self._google_blocked:
                return await self._direct_warmup_visit(page, url, context=context)

            if not self._can_use_google(for_host=host):
                if is_target or first_visit:
                    self._report("Google budget spent on ambient — still searching Google for target")
                else:
                    self._report(f"Skipping {host} — Google search budget spent")
                    return False

            q = query if attempt == 0 else self._url_to_search_query(url)
            arrived = await self.search_and_visit_site(
                page, url, extra_query=q, context=context)
            if arrived:
                return True
            if self._google_blocked:
                return await self._direct_warmup_visit(page, url, context=context)
            if attempt < retries:
                self._report(f"Google miss for {host} — retrying with another query")
                await asyncio.sleep(random.uniform(3, 8))

        if self._google_blocked:
            return await self._direct_warmup_visit(page, url, context=context)
        self._report(f"Could not reach {host} via Google")
        return False

    async def _search_engine_visit(self, page, url: str, query: str,
                                    engine: str = "google") -> bool:
        """Google arrival helper; falls back to a direct visit if Google is blocked."""
        _ = engine
        if self._google_blocked:
            return await self._direct_warmup_visit(page, url)
        host = self._clean_search_host(url)
        if not self._can_use_google(for_host=host):
            self._report(f"Skipping {host} — Google search budget spent")
            return False
        return await self.search_and_visit_site(page, url, extra_query=query)

    async def search_and_visit_site(self, page, url: str, extra_query: str = None,
                                     context=None, depth_override: tuple = None) -> bool:
        """
        Navigate to a website by searching Google first, then clicking through.

        Full organic search workflow:
        1. Open Google
        2. Type a randomized search query with human typing speed
        3. Wait for results to fully load
        4. Scroll through results naturally with mouse movements
        5. Occasionally click 1-2 unrelated results first (then go back)
        6. Identify and click the correct domain
        7. If not found on page 1 → paginate to page 2
        8. If wrong domain clicked → go back and retry
        9. On arrival: wait for full page load (JS, tracking, cookies)
        10. Dwell for realistic duration, then deep-explore

        Returns True if the target site was reached via search.
        """
        self._check_stop()  # Hard stop check
        try:
            parsed = urlparse(url)
            target_domain = (parsed.netloc or parsed.path.split("/")[0]).replace("www.", "").lower()
        except Exception:
            target_domain = url.lower()

        # Strip to core domain for matching (e.g., "openrouter.ai" → "openrouter")
        target_domain_core = target_domain.split(".")[0]

        # Generate a randomized search query — never type a raw URL/path.
        query = extra_query or self._url_to_search_query(url)
        if not query or "/" in query or "http" in query.lower() or len(query) > 80:
            query = self._url_to_search_query(url)

        # ── Google blocked (CAPTCHA) → open the site directly ──
        if self._google_blocked:
            return await self._direct_warmup_visit(
                page, url, context=context, depth_override=depth_override)

        # Budget applies to ambient sites; target always searches Google.
        if not self._can_use_google(for_host=target_domain):
            self._report(f"Skipping {target_domain} — Google search budget spent")
            return False
        await self._await_search_slot()
        self._google_search_count += 1

        self._report(f"Typing \"{query}\" into Google search...")

        # ── STEP 1: Warm up Google session (build cookies/history) ────
        if not hasattr(self, '_google_warmed_up'):
            await self.warmup_google_session(page, context)
            # Extra delay after warmup before searching
            await asyncio.sleep(random.uniform(3, 8))
        
        # ── STEP 1: Navigate to Google ────────────────────────
        # Don't always go directly — sometimes we're already there or come from elsewhere
        current_url = page.url.lower()
        is_already_on_google = "google.com" in current_url
        
        if not is_already_on_google:
            # 15% chance to come from another site first (more natural)
            if random.random() < 0.15:
                referrer_sites = [
                    "https://www.wikipedia.org",
                    "https://www.reddit.com",
                ]
                ref = random.choice(referrer_sites)
                await self.safe_navigate(page, ref)
                await asyncio.sleep(random.uniform(2, 5))
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(1, 3))
            
            ok = await self.safe_navigate(page, "https://www.google.com")
            if not ok:
                logger.info("Google unreachable — opening target directly")
                self._report(
                    f"Google unreachable — opening {target_domain} directly")
                self._mark_google_blocked(persist=False)
                return await self._direct_warmup_visit(
                    page, url, context=context, depth_override=depth_override)
        else:
            # Already on Google — just wait (like user is thinking)
            await asyncio.sleep(random.uniform(2, 5))

        # Human pause: look at the page before interacting
        await asyncio.sleep(random.uniform(2.0, 5.0))
        await self.move_mouse_randomly(page)

        # ── STEP 2: Type the search query with human speed ────
        typed = False
        for selector in ['textarea[name="q"]', 'input[name="q"]']:
            success = await self.type_like_human(page, selector, query)
            if success:
                typed = True
                break

        if not typed:
            # Fallback: direct search URL (still generates search cookies)
            await self.safe_navigate(
                page, f"https://www.google.com/search?q={query.replace(' ', '+')}"
            )
            await self.random_sleep(2, 4)
        else:
            # Pause — reading autocomplete suggestions
            await asyncio.sleep(random.uniform(1.0, 3.5))

            # 15% chance: refine the query like a real person would
            if random.random() < 0.15:
                additions = [" official", " site", " .com", " review",
                             f" {datetime.now().year}", " platform",
                             " official website", " home"]
                extra_text = random.choice(additions)
                for char in extra_text:
                    await page.keyboard.type(char, delay=random.uniform(70, 220))
                await asyncio.sleep(random.uniform(0.5, 1.5))

            # 8% chance: typo → backspace → correct (very human) - increased for realism
            if random.random() < 0.08:
                typo_chars = random.randint(1, 3)
                for _ in range(typo_chars):
                    await page.keyboard.type(
                        random.choice("abcdefghijklmnopqrstuvwxyz"),
                        delay=random.uniform(70, 180),
                    )
                await asyncio.sleep(random.uniform(0.4, 1.2))
                for _ in range(typo_chars):
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(80, 200) / 1000)

            await page.keyboard.press("Enter")

            # Wait for results to FULLY load (including tracking scripts)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                try:
                    await page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(2.0, 4.5))

        # ── STEP 3: Engage with search results page ──────────
        await self.dismiss_popups(page)

        # Check for Google sorry/CAPTCHA page before proceeding
        # force_solve=True: search results are the only path to the target here
        if not await self.detect_and_solve_captcha(page, force_solve=True):
            self._report(
                f"Google CAPTCHA — opening {target_domain} directly")
            return await self._direct_warmup_visit(
                page, url, context=context, depth_override=depth_override)

        # Natural SERP behavior: scroll down slowly, move mouse, scan results
        self._report(f"Scanning Google results — looking for {target_domain}...")

        # Multiple small scrolls with pauses (scanning behavior)
        for _ in range(random.randint(1, 3)):
            try:
                scroll_amt = random.randint(150, 350)
                await page.mouse.wheel(0, scroll_amt)
                await asyncio.sleep(random.uniform(0.8, 2.5))
                await self.move_mouse_randomly(page)
            except Exception:
                break

        await asyncio.sleep(random.uniform(0.5, 2.0))

        # ── STEP 4: Occasionally click 1-2 unrelated results first ──
        if random.random() < 0.25:
            await self._click_unrelated_results(page, target_domain)

        # ── STEP 5: Find and click the target result ──────────
        clicked = await self._find_and_click_target_in_results(page, target_domain)

        if not clicked:
            # Scroll more to reveal lower results
            for _ in range(random.randint(1, 2)):
                try:
                    await page.mouse.wheel(0, random.randint(300, 600))
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                except Exception:
                    break
            clicked = await self._find_and_click_target_in_results(page, target_domain)

        # ── STEP 6: Try page 2 if not found ───────────────────
        if not clicked:
            self._report(f"{target_domain} not found on page 1 — scrolling to page 2...")
            try:
                next_selectors = [
                    'a#pnnext',
                    'a[aria-label="Next"]',
                    'a[aria-label="Page 2"]',
                    '#botstuff a[href*="start=10"]',
                ]
                next_btn = None
                for sel in next_selectors:
                    next_btn = await page.query_selector(sel)
                    if next_btn:
                        try:
                            if await next_btn.is_visible():
                                break
                        except Exception:
                            next_btn = None
                            continue

                if next_btn:
                    await next_btn.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                    await self.move_mouse_randomly(page)
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                    await next_btn.click()

                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    await self.dismiss_popups(page)

                    # Scan page 2
                    for _ in range(random.randint(1, 2)):
                        try:
                            await page.mouse.wheel(0, random.randint(200, 400))
                            await asyncio.sleep(random.uniform(0.8, 2.0))
                        except Exception:
                            break

                    clicked = await self._find_and_click_target_in_results(page, target_domain)
            except Exception:
                pass

        # ── STEP 7: Retry with different query if still not found ─
        if not clicked:
            # Generate a different query variation and retry once
            alt_query = self._url_to_search_query(url)
            # Make sure it's different from the first attempt
            for _ in range(5):
                if alt_query != query:
                    break
                alt_query = self._url_to_search_query(url)

            if alt_query != query:
                self._report(f"Not found — retrying with different query: \"{alt_query}\"")
                # Use safe_navigate for retry (handles Cloudflare, CAPTCHA, etc.)
                try:
                    search_url = f"https://www.google.com/search?q={alt_query.replace(' ', '+')}"
                    ok = await self.safe_navigate(page, search_url)
                    if not ok:
                        pass  # Fall through to "target not found"
                    await asyncio.sleep(random.uniform(2.5, 5.0))
                    await self.dismiss_popups(page)

                    for _ in range(random.randint(1, 2)):
                        try:
                            await page.mouse.wheel(0, random.randint(200, 400))
                            await asyncio.sleep(random.uniform(0.8, 2.0))
                        except Exception:
                            break

                    clicked = await self._find_and_click_target_in_results(page, target_domain)
                except Exception:
                    pass

        if not clicked:
            self._report(f"Target not found in Google results")
            logger.debug(f"search_and_visit: couldn't find {target_domain} in Google")
            return False

        # ── STEP 8: Verify we landed on the correct site ──────
        await self._wait_full_page_load(page)

        # Wrong domain recovery: if we're on the wrong site, go back
        try:
            current_url = page.url.lower()
            if target_domain not in current_url and target_domain_core not in current_url:
                self._report(f"Wrong site — going back to results")
                logger.debug(f"Landed on {current_url} instead of {target_domain}")
                await page.go_back()
                await asyncio.sleep(random.uniform(2, 4))
                await self.dismiss_popups(page)
                # Try once more
                clicked = await self._find_and_click_target_in_results(page, target_domain)
                if not clicked:
                    return False
                await self._wait_full_page_load(page)
        except Exception:
            pass

        # ── STEP 9: We arrived! Realistic dwell + cookie generation ─
        self._report(f"Landed on {target_domain} — dwelling on page, loading cookies...")
        await self.dismiss_popups(page)

        # Realistic dwell — deep enough for cookies, not a long sit
        await self._realistic_dwell(page, min_s=2, max_s=5)

        # ── STEP 10: Deep explore the site via internal links ──
        if context and depth_override:
            d_min, d_max = int(depth_override[0]), int(depth_override[1])
            if d_min > d_max:
                d_min, d_max = d_max, d_min
            if d_min > 0:
                await self._deep_explore_current_page(
                    page, context, target_domain,
                    depth=random.randint(d_min, d_max),
                )
        else:
            # Default exploration: read + interact + scroll
            await self.simulate_reading(page)
            if random.random() < 0.6:
                await self.interact_with_page_forms(page)
            if random.random() < 0.4:
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(1.0, 3.0))

        # Final engagement: type in fields, click login/CTA before leaving
        if not page.is_closed() and random.random() < 0.45:
            try:
                await self.engage_with_site_ui(page)
            except Exception:
                pass
        try:
            await self.maybe_shop_cart(page)
        except (_StopRequested, _SkipPhase):
            raise
        except Exception:
            pass

        return True

    async def _click_unrelated_results(self, page, target_domain: str):
        """
        Occasionally click 1-2 unrelated search results before the target.
        This simulates natural search behavior — users don't always click
        the right result immediately. They explore, read snippets, click
        one or two wrong ones, go back, then find what they need.
        """
        try:
            results = await self._collect_search_results(page)
            if not results:
                return

            # Find non-target results
            unrelated = [
                (el, href) for el, href in results
                if target_domain not in href.lower()
                and "google.com" not in href.lower()
            ]

            if not unrelated:
                return

            # Click 1-2 unrelated results
            num_clicks = random.randint(1, min(2, len(unrelated)))
            for el, href in random.sample(unrelated, num_clicks):
                try:
                    self._report("Clicking an unrelated result first — looks more natural")
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await self.move_mouse_randomly(page)
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await el.hover()
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                    await el.click()

                    # Wait for the unrelated page to load (get cookies from it too)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        try:
                            await page.wait_for_load_state("load", timeout=6000)
                        except Exception:
                            pass

                    # Brief dwell on the unrelated site
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                    await self.scroll_page(page)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

                    # Go back to search results
                    self._report("Going back to Google results — now looking for the real target")
                    await page.go_back()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                    await self.dismiss_popups(page)

                except Exception:
                    # If we can't go back, try navigating to results
                    try:
                        await page.go_back()
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    break

        except Exception as e:
            logger.debug(f"Unrelated result click error: {e}")

    async def _find_and_click_target_in_results(self, page, target_domain: str) -> bool:
        """
        Scan Google search results for a link matching target_domain.

        Simulates natural human scanning behavior:
        - Reads results top to bottom (not instant jumps)
        - Hovers over several results (reading titles/snippets)
        - Mouse movements between results
        - Does NOT always select the first match
        - Scrolls the result into view before clicking
        """
        try:
            results = await self._collect_search_results(page)
            if not results:
                return False

            # Separate target matches from non-target results
            non_target = []
            target_results = []

            for el, href in results:
                href_lower = href.lower()
                target_core = target_domain.split(".")[0]
                if target_domain in href_lower or target_core in href_lower:
                    target_results.append((el, href))
                else:
                    non_target.append((el, href))

            if not target_results:
                return False

            # ── Human scanning simulation ────────────────────
            # Read through 2-4 results before clicking (natural SERP scanning)
            scan_count = random.randint(1, min(4, len(non_target) + len(target_results)))
            scanned = 0

            # Scan some non-target results first (reading titles)
            for el, href in non_target[:scan_count]:
                try:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await el.hover()
                    # Brief pause — reading the snippet
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                    scanned += 1
                    if scanned >= scan_count:
                        break
                except Exception:
                    continue

            # Move mouse randomly (transition between scanning and clicking)
            await self.move_mouse_randomly(page)
            await asyncio.sleep(random.uniform(0.3, 1.0))

            # Pick which target result to click (not always the first one)
            target_el, target_href = random.choice(target_results)

            # Scroll target result into view
            try:
                await target_el.scroll_into_view_if_needed()
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.4, 1.2))

            # Mouse movement toward the result (not instant teleport)
            await target_el.hover()
            await asyncio.sleep(random.uniform(0.4, 1.5))

            # Click
            await target_el.click()

            # Wait for full page load (JS + tracking + cookies)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                try:
                    await page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Verify we landed on the target domain
            try:
                current_url = page.url.lower()
                target_core = target_domain.split(".")[0]
                if target_domain in current_url or target_core in current_url:
                    return True
            except Exception:
                pass

            # If URL verification failed but we clicked, still count it
            return True

        except Exception as e:
            logger.debug(f"_find_and_click_target error: {e}")
            return False

    async def _deep_explore_current_page(self, page, context, domain: str, depth: int = 3):
        """
        After arriving at a site via Google, explore it deeply by clicking
        internal links in a long chain. Designed to go 10-15+ pages deep.

        Each page visit:
        - Waits for full load (networkidle) for cookie/tracking scripts
        - Dwells for a realistic duration
        - Scrolls, moves mouse, hovers links
        - Clicks an internal link to go deeper
        - If stuck, uses go_back + different branch strategy
        """
        visited_urls = set()
        try:
            current = page.url
            visited_urls.add(current.split("?")[0].split("#")[0].rstrip("/"))
        except Exception:
            pass

        domain_core = domain.split(".")[0]
        consecutive_failures = 0
        pages_explored = 0
        self._report(f"Deep exploring {domain} — planning to visit {depth} internal pages")

        for i in range(depth):
            self._check_skip()

            if page.is_closed():
                break

            # ── Full page load + realistic dwell ──────────────
            await self._wait_full_page_load(page, timeout_ms=15000)
            await self.dismiss_popups(page)

            # Realistic time on page (reading, scrolling, hovering)
            dwell_min = random.uniform(2, 5)
            dwell_max = random.uniform(5, 12)
            await self._realistic_dwell(page, min_s=dwell_min, max_s=dwell_max)

            # Interact with forms / UI elements on some pages
            if random.random() < 0.30:
                await self.interact_with_page_forms(page)
            if random.random() < 0.15:
                await self.engage_with_site_ui(page)
            try:
                await self.maybe_shop_cart(page)
            except (_StopRequested, _SkipPhase):
                raise
            except Exception:
                pass

            # ── Find internal links — scan ALL links, resolve relative URLs ──
            candidate_links = await self._find_internal_links(
                page, domain, domain_core, visited_urls
            )

            # If nothing found, try scrolling to reveal lazy-loaded content
            if not candidate_links:
                for scroll_attempt in range(3):
                    try:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight * %s)"
                            % (0.3 + scroll_attempt * 0.25)
                        )
                    except Exception:
                        break
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    candidate_links = await self._find_internal_links(
                        page, domain, domain_core, visited_urls
                    )
                    if candidate_links:
                        break

            if not candidate_links:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    # Stuck — go back and try a different branch
                    try:
                        self._report(f"Dead end (page {pages_explored}/{depth}) — going back to try a different path")
                        await page.go_back()
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        await self.dismiss_popups(page)
                        consecutive_failures = 0
                        continue
                    except Exception:
                        break
                continue

            consecutive_failures = 0

            # Separate content links (articles, docs, blog) from nav links
            content_links = []
            nav_links = []
            for link, href, text in candidate_links:
                text_lower = text.lower()
                href_lower = href.lower()
                if any(kw in text_lower or kw in href_lower for kw in self._PREFERRED_KEYWORDS):
                    content_links.append((link, href, text))
                else:
                    nav_links.append((link, href, text))

            # Prefer content links (70% of the time), fall back to nav
            if content_links and random.random() < 0.70:
                pool = content_links
            else:
                pool = candidate_links

            # Pick from the full pool — not just the first 10
            chosen_link, chosen_href, chosen_text = random.choice(pool)

            self._report(f"  Deep [{pages_explored+1}/{depth}] Clicking: {chosen_text[:40]} ({domain})")
            try:
                await chosen_link.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 1.0))
                await self.move_mouse_randomly(page)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await chosen_link.hover()
                await asyncio.sleep(random.uniform(0.2, 0.8))
                await chosen_link.click()

                # Wait for full load on internal page
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    try:
                        await page.wait_for_load_state("load", timeout=8000)
                    except Exception:
                        pass

                await asyncio.sleep(random.uniform(1.0, 3.0))

                # Record the URL we actually arrived at (not just the href)
                try:
                    actual_url = page.url
                    visited_urls.add(actual_url.split("?")[0].split("#")[0].rstrip("/"))
                except Exception:
                    pass
                visited_urls.add(chosen_href.split("?")[0].split("#")[0].rstrip("/"))

                # Handle obstacles
                await self.dismiss_popups(page)

                if await self.is_dead_page(page):
                    self._report("Dead page — going back")
                    try:
                        await page.go_back()
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                    continue

                pages_explored += 1

                if not await self.detect_and_solve_captcha(page):
                    self._report("Captcha unsolvable — stopping exploration")
                    break

                # Check we're still on the same domain
                try:
                    curr = page.url.lower()
                    if domain not in curr and domain_core not in curr:
                        self._report(f"Accidentally left {domain} — navigating back")
                        await page.go_back()
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                        continue
                except Exception:
                    pass

                # Occasionally branch: go back and try a different path
                if random.random() < 0.15 and pages_explored > 2:
                    try:
                        await page.go_back()
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                        await self.dismiss_popups(page)
                    except Exception:
                        pass

            except Exception as e:
                logger.debug(f"Deep explore click failed: {e}")
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    try:
                        await page.go_back()
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                        consecutive_failures = 0
                    except Exception:
                        break
                continue

        self._report(f"Deep explore complete — visited {pages_explored}/{depth} pages on {domain}")

        # ── Final engagement: interact with current page before leaving ──
        if not page.is_closed() and random.random() < 0.55:
            try:
                await self.engage_with_site_ui(page)
            except Exception:
                pass

    async def _find_internal_links(self, page, domain: str, domain_core: str,
                                    visited_urls: set) -> list:
        """
        Scan ALL links on the page and return internal, unvisited, non-blocked ones.
        Handles both absolute and relative URLs. Returns list of (element, href, text).
        """
        candidates = []
        try:
            links = await page.query_selector_all("a[href]")
        except Exception:
            return candidates

        # Get the current page URL for resolving relative links
        try:
            page_url = page.url
            page_parsed = urlparse(page_url)
            page_origin = f"{page_parsed.scheme}://{page_parsed.netloc}"
        except Exception:
            page_origin = ""

        for link in links[:150]:  # Scan up to 150 links (not just 50)
            try:
                if not await link.is_visible():
                    continue
                href = await link.get_attribute("href") or ""
                if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
                    continue

                # Resolve relative URLs to absolute
                if href.startswith("/"):
                    href = page_origin + href
                elif not href.startswith("http"):
                    href = page_origin + "/" + href

                href_lower = href.lower()
                href_normalized = href.split("?")[0].split("#")[0].rstrip("/")

                # Must be internal (same domain), not visited, not blocked
                if not (domain in href_lower or domain_core in href_lower):
                    continue
                if href_normalized in visited_urls:
                    continue
                if self._is_blocked_link(href, ""):
                    continue

                text = ""
                try:
                    text = (await link.inner_text() or "").strip()
                except Exception:
                    pass
                if not text or len(text) < 2:
                    continue

                # Skip links that look like the same page (anchors / fragments)
                if text.lower() in ("back to top", "skip to content", "↑"):
                    continue

                candidates.append((link, href, text))
            except Exception:
                continue

        return candidates

    # ══════════════════════════════════════════════════════════════
    #  LINK INTERACTION
    # ══════════════════════════════════════════════════════════════

    async def hover_random_element(self, page):
        """Hover over a random visible link."""
        try:
            links = await page.query_selector_all("a[href]")
            if not links:
                return
            visible_links = []
            for link in links[:30]:
                try:
                    if await link.is_visible():
                        visible_links.append(link)
                except Exception:
                    continue
            if visible_links:
                target = random.choice(visible_links[:15])
                await target.hover()
                await asyncio.sleep(random.uniform(0.5, 2.0))
        except Exception as e:
            logger.debug(f"Hover error: {e}")

    # Words in href or link text that signal auth/registration walls
    _BLOCKED_KEYWORDS = {
        "login", "log-in", "log_in", "signin", "sign-in", "sign_in",
        "signup", "sign-up", "sign_up", "register", "registration",
        "auth", "oauth", "sso", "callback", "redirect",
        "account", "my-account", "myaccount", "profile/settings",
        "password", "forgot", "reset-password", "verify",
        "subscribe", "premium", "upgrade", "checkout", "cart",
        "download", "install", "mailto:", "tel:",
        "javascript:", "#",
        "logout", "log-out", "signout", "sign-out",
    }

    # Words in href or link text that signal good explorable content
    _PREFERRED_KEYWORDS = {
        "docs", "documentation", "guide", "tutorial", "learn",
        "pricing", "plans", "features", "about", "blog", "news",
        "models", "explore", "trending", "popular", "top",
        "faq", "help", "support", "community", "forum",
        "api", "reference", "examples", "demo", "showcase",
        "changelog", "updates", "release", "articles",
        "templates", "marketplace", "catalog", "products",
        "compare", "benchmark", "leaderboard", "rankings",
        "resources", "tools", "integrations", "partners",
        "serverless", "gpu", "pods", "deploy", "console",
    }

    def _is_blocked_link(self, href: str, text: str) -> bool:
        """Check if a link leads to auth/registration or other dead ends."""
        combined = (href + " " + text).lower()
        return any(kw in combined for kw in self._BLOCKED_KEYWORDS)

    def _is_preferred_link(self, href: str, text: str) -> bool:
        """Check if a link leads to explorable content."""
        combined = (href + " " + text).lower()
        return any(kw in combined for kw in self._PREFERRED_KEYWORDS)

    async def _gather_links(self, page, visited_urls: set = None):
        """
        Gather all clickable content links on the page.
        Returns (preferred, fallback) sorted by quality.
        Excludes already-visited URLs.
        """
        if visited_urls is None:
            visited_urls = set()

        preferred_links = []
        fallback_links = []

        try:
            if page.is_closed():
                return preferred_links, fallback_links

            current_domain = urlparse(page.url).netloc

            links = await page.query_selector_all("a[href]")
            for link in links[:60]:  # Check more links for better selection
                try:
                    href = await link.get_attribute("href") or ""
                    if not href or not await link.is_visible():
                        continue

                    text = (await link.inner_text() or "").strip()

                    # Skip auth/registration links
                    if self._is_blocked_link(href, text):
                        continue

                    # Skip already-visited URLs
                    try:
                        clean_href = href.split("?")[0].split("#")[0].rstrip("/")
                        if clean_href in visited_urls:
                            continue
                    except Exception:
                        pass

                    # Skip external social media links
                    if any(d in href for d in [
                        "facebook.com", "twitter.com", "x.com", "instagram.com",
                        "tiktok.com", "discord.gg", "t.me", "linkedin.com",
                    ]):
                        continue

                    # Skip anchors, javascript, and very short text
                    if href.startswith("#") or href.startswith("javascript:"):
                        continue
                    if len(text) < 2 and not self._is_preferred_link(href, ""):
                        continue

                    # Skip file downloads
                    if any(href.lower().endswith(ext) for ext in [
                        ".pdf", ".zip", ".exe", ".dmg", ".msi", ".tar", ".gz",
                        ".mp4", ".mp3", ".avi", ".mov", ".png", ".jpg", ".jpeg",
                    ]):
                        continue

                    # Prefer same-domain links (stay on the site)
                    try:
                        link_domain = urlparse(href).netloc
                        is_same_domain = (
                            not link_domain
                            or link_domain == current_domain
                            or link_domain.endswith("." + current_domain)
                            or current_domain.endswith("." + link_domain)
                        )
                    except Exception:
                        is_same_domain = True

                    # Categorize
                    if self._is_preferred_link(href, text):
                        preferred_links.append((link, href, is_same_domain))
                    elif is_same_domain:
                        fallback_links.append((link, href, is_same_domain))

                except Exception:
                    continue

            # Sort: same-domain first
            preferred_links.sort(key=lambda x: not x[2])
            fallback_links.sort(key=lambda x: not x[2])

        except Exception as e:
            logger.debug(f"Gather links error: {e}")

        return preferred_links, fallback_links

    async def _click_content_link(self, page, visited_urls: set = None):
        """
        Click a content link on the page, avoiding auth/registration walls.
        Tracks visited URLs to avoid loops.
        Returns (success: bool, clicked_href: str or None).
        """
        try:
            preferred, fallback = await self._gather_links(page, visited_urls)

            candidates = preferred if preferred else fallback
            if not candidates:
                return False, None

            # Pick from top candidates (weighted toward first = best)
            pool_size = min(len(candidates), 10)
            target_link, href, _ = random.choice(candidates[:pool_size])

            await target_link.hover()
            await asyncio.sleep(random.uniform(0.3, 1.2))
            await target_link.click()
            await asyncio.sleep(random.uniform(1.5, 4.0))
            return True, href
        except Exception as e:
            logger.debug(f"Click link error: {e}")
        return False, None

    # Kept for backward compatibility
    # ══════════════════════════════════════════════════════════════
    #  TAB MANAGEMENT
    # ══════════════════════════════════════════════════════════════

    async def switch_to_random_tab(self, context):
        """Switch to a random open tab."""
        try:
            pages = [p for p in context.pages if not p.is_closed()]
            if len(pages) > 1:
                target = random.choice(pages)
                await self._ensure_tab_visible(target)
                await self.random_sleep(1, 3)
                return target
        except Exception as e:
            logger.debug(f"Tab switch error: {e}")
        return None

    # ══════════════════════════════════════════════════════════════
    #  VIEWPORT
    # ══════════════════════════════════════════════════════════════

    async def sync_viewport_to_window(self, page):
        """Use the real AdsPower window — do not emulate a smaller laptop box."""
        if page is None or page.is_closed():
            return
        try:
            cdp = await page.context.new_cdp_session(page)
            try:
                await cdp.send("Emulation.clearDeviceMetricsOverride")
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Clear device metrics: {e}")
        try:
            size = await page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})"
            )
            if size and size.get("w") and size.get("h"):
                self._report(f"Viewport: {int(size['w'])}x{int(size['h'])} (window)")
                logger.info(
                    f"Viewport synced to window {int(size['w'])}x{int(size['h'])}"
                )
        except Exception as e:
            logger.debug(f"Viewport read error: {e}")

    async def set_random_viewport(self, page):
        """Back-compat alias — match the real window instead of a fake size."""
        await self.sync_viewport_to_window(page)

    # ══════════════════════════════════════════════════════════════
    #  TEXT SELECTION (humans sometimes select text while reading)
    # ══════════════════════════════════════════════════════════════

    async def select_random_text(self, page):
        """Highlight/select a random piece of text (common human behavior)."""
        try:
            # Find text-heavy elements
            paragraphs = await page.query_selector_all("p, h2, h3, li, span")
            visible = []
            for el in paragraphs[:20]:
                try:
                    if await el.is_visible():
                        text = (await el.inner_text() or "").strip()
                        if len(text) > 20:
                            visible.append(el)
                except Exception:
                    continue

            if visible:
                target = random.choice(visible[:8])
                box = await target.bounding_box()
                if box:
                    # Triple-click to select the line/paragraph
                    x = box["x"] + random.randint(5, int(box["width"] * 0.3))
                    y = box["y"] + box["height"] / 2
                    if random.random() < 0.5:
                        await page.mouse.click(x, y, click_count=2)  # Select word
                    else:
                        await page.mouse.click(x, y, click_count=3)  # Select paragraph
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                    # Deselect by clicking elsewhere
                    await page.mouse.click(x + 50, y + 30)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    #  ZOOM (some people zoom in/out)
    # ══════════════════════════════════════════════════════════════

    async def random_zoom(self, page):
        """Occasionally zoom in or out, then back to normal."""
        try:
            # Ctrl+Plus to zoom in
            await page.keyboard.down("Control")
            action = random.choice(["in", "out"])
            key = "+" if action == "in" else "-"
            await page.keyboard.press(key)
            await page.keyboard.up("Control")
            await asyncio.sleep(random.uniform(1.0, 3.0))
            # Reset zoom
            await page.keyboard.down("Control")
            await page.keyboard.press("0")
            await page.keyboard.up("Control")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    #  COMBINED BEHAVIORS — rich realistic patterns
    # ══════════════════════════════════════════════════════════════

    async def simulate_reading(self, page):
        """
        Simulate a real human reading a page.
        Randomly combines multiple behaviors in varied order.
        """
        self._check_skip()
        self._report("Reading page — scrolling, looking around...")
        # Dismiss any leftover popups first
        await self.dismiss_popups(page)

        # Pick a random subset of actions (not the same every time)
        actions = []
        actions.append("scroll")  # Always scroll

        if random.random() < 0.7:
            actions.append("mouse_move")
        if random.random() < 0.35:
            actions.append("hover_link")
        if random.random() < 0.2:
            actions.append("select_text")
        if random.random() < 0.05:
            actions.append("zoom")
        if random.random() < 0.5:
            actions.append("pause")
        if random.random() < 0.3:
            actions.append("mouse_move_2")

        random.shuffle(actions)

        for action in actions:
            try:
                if action == "scroll":
                    await self.scroll_page(page)
                elif action == "mouse_move" or action == "mouse_move_2":
                    await self.move_mouse_randomly(page)
                elif action == "hover_link":
                    await self.hover_random_element(page)
                elif action == "select_text":
                    await self.select_random_text(page)
                elif action == "zoom":
                    await self.random_zoom(page)
                elif action == "pause":
                    await self.thinking_pause()
            except Exception:
                continue

        # Final reading pause — variable duration
        await self.random_sleep(2, 8)

    async def _wait_for_page_settled(self, page, timeout_s: float = 5.0):
        """Wait for page to finish loading dynamic content."""
        if page.is_closed():
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
        except Exception:
            pass
        # Extra settle time for JS-heavy sites
        await asyncio.sleep(random.uniform(0.5, 2.0))

    async def _scroll_to_reveal_links(self, page):
        """Scroll down to trigger lazy-loaded content, then scroll back."""
        try:
            # Scroll to ~60% of page to trigger lazy loads
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
            await asyncio.sleep(random.uniform(1.0, 2.5))
            # Scroll back to where we were
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.2)")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass

    async def browse_site(self, page, url: str, context=None, depth_override: tuple = None):
        """
        Navigate to a site and do deep, thorough human-like exploration.

        Strategy:
        - Track every visited URL to never revisit the same page
        - On each page: dismiss obstacles, read, then find links
        - If click fails: scroll to reveal more links, retry up to 3 times
        - If still stuck: go back and try a different branch
        - Support branching: explore one path, go back, explore another
        - Handle tab death, Cloudflare, captchas, auth redirects at every step
        """
        visited_urls = set()
        consecutive_failures = 0
        max_consecutive_failures = 3

        # Depth range — how many pages deep to go
        if depth_override:
            depth_min, depth_max = int(depth_override[0]), int(depth_override[1])
        else:
            depth_min, depth_max = 6, 12
        if depth_min > depth_max:
            depth_min, depth_max = depth_max, depth_min
        target_depth = random.randint(max(1, depth_min), max(1, depth_max))

        try:
            domain = urlparse(url).netloc or url[:40]
            self._report(f"Browsing {domain} — clicking through {target_depth} pages")

            # ── Initial navigation ─────────────────────────────
            ok = await self.safe_navigate(page, url)
            if not ok:
                return

            # Record landing page
            if not page.is_closed():
                try:
                    visited_urls.add(page.url.split("?")[0].split("#")[0].rstrip("/"))
                except Exception:
                    pass

            await self._wait_for_page_settled(page)
            try:
                host = urlparse(page.url).netloc.replace("www.", "")
            except Exception:
                host = ""
            mode_budget = max(2, min(target_depth, random.randint(3, 6)))
            try:
                await self._explore_observed_site(
                    page, context, host,
                    max_pages=mode_budget, item_terms=None, metrics=None,
                )
            except _SkipPhase:
                raise
            except Exception as e:
                logger.debug(f"Observed browse from browse_site: {e}")

            pages_explored = mode_budget

            # ── Extra hops if the observer used less than the depth budget ──
            leftover = max(0, target_depth - pages_explored)
            for step in range(leftover):
                self._check_skip()

                if page.is_closed():
                    logger.info("Page closed during exploration — moving on")
                    return

                # Clear any popups before trying to click
                await self.dismiss_popups(page)

                # Try to click a link (with retries)
                clicked = False
                for attempt in range(3):
                    if page.is_closed():
                        return

                    if context:
                        clicked = await self.safe_click_link(page, context, visited_urls)
                    else:
                        success, href = await self._click_content_link(page, visited_urls)
                        clicked = success
                        if href:
                            try:
                                visited_urls.add(href.split("?")[0].split("#")[0].rstrip("/"))
                            except Exception:
                                pass

                    if clicked:
                        break

                    # Click failed — try to reveal more content
                    if attempt == 0:
                        # First retry: scroll to reveal lazy-loaded links
                        await self._scroll_to_reveal_links(page)
                        await asyncio.sleep(random.uniform(1, 2))
                    elif attempt == 1:
                        # Second retry: scroll all the way down
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(random.uniform(1.5, 3))
                        await self.dismiss_popups(page)

                if clicked and not page.is_closed():
                    consecutive_failures = 0
                    pages_explored += 1

                    # Wait for new page to settle
                    await self._wait_for_page_settled(page)

                    # Record URL
                    if not page.is_closed():
                        try:
                            current_url = page.url
                            visited_urls.add(current_url.split("?")[0].split("#")[0].rstrip("/"))
                            cur_domain = urlparse(current_url).netloc
                            self._report(f"  Browse [{pages_explored}/{target_depth}] Reading page on {cur_domain}")
                        except Exception:
                            pass

                    # Read this new page
                    await self.simulate_reading(page)

                    # ── Branching strategy ─────────────────────
                    # Sometimes go back and try a different path
                    if random.random() < 0.4 and step < target_depth - 1:
                        try:
                            await page.go_back()
                            await asyncio.sleep(random.uniform(1.5, 4))
                            await self.dismiss_popups(page)
                            await self._wait_for_page_settled(page)

                            # Quick read of the page we returned to
                            if random.random() < 0.5:
                                await self.scroll_page(page)
                        except Exception:
                            pass
                else:
                    # All retries failed
                    consecutive_failures += 1

                    if consecutive_failures >= max_consecutive_failures:
                        # We're stuck — try going back to find new links
                        try:
                            await page.go_back()
                            await asyncio.sleep(random.uniform(1, 3))
                            await self.dismiss_popups(page)
                            consecutive_failures = 0  # Reset after going back
                        except Exception:
                            break  # Can't go back either — give up on this site

                # Small random delay between pages
                await asyncio.sleep(random.uniform(1, 4))

            # ── Final wrap-up ──────────────────────────────────
            if not page.is_closed():
                # Engage with the last page: type in fields, click login/CTA
                if random.random() < 0.50:
                    try:
                        await self.engage_with_site_ui(page)
                    except Exception:
                        pass

                # Sometimes navigate back through history
                if random.random() < 0.3 and pages_explored > 1:
                    try:
                        for _ in range(random.randint(1, min(3, pages_explored))):
                            await page.go_back()
                            await asyncio.sleep(random.uniform(1, 3))
                    except Exception:
                        pass

                logger.debug(
                    f"Explored {pages_explored} pages on {urlparse(url).netloc} "
                    f"({len(visited_urls)} unique URLs)"
                )

        except Exception as e:
            logger.debug(f"Browse site error ({url}): {e}")

    # ══════════════════════════════════════════════════════════════
    #  BANDWIDTH SAVER
    # ══════════════════════════════════════════════════════════════

    async def enable_bandwidth_saver(self, context):
        """
        Block images, fonts, and heavy media on ALL pages in the context.

        Applied at the BrowserContext level so every new tab automatically
        inherits the rules — no need to re-apply per page.

        Typical savings: ~70-80% less data per profile.
        Pages still load HTML, CSS, and JS — cookies, history, and
        engagement signals are all preserved.  Only visual assets are stripped.
        """
        async def _abort_asset(route):
            await route.abort()

        try:
            # Block images (jpg, png, gif, webp, svg, ico, avif)
            await context.route(
                "**/*.{jpg,jpeg,png,gif,webp,svg,ico,avif,bmp,tiff}", _abort_asset
            )
            # Block font files
            await context.route("**/*.{woff,woff2,ttf,otf,eot}", _abort_asset)
            # Block video/audio
            await context.route(
                "**/*.{mp4,webm,m4s,ts,m3u8,mpd,mp3,ogg,wav,flac}", _abort_asset
            )
            await context.route("**/videoplayback*", _abort_asset)
            await context.route("**/googlevideo.com/**", _abort_asset)
            logger.debug("Bandwidth saver enabled on context — images, fonts & media blocked")
        except Exception as e:
            logger.debug(f"Bandwidth saver setup error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  YOUTUBE WATCHING  (#4)
    # ══════════════════════════════════════════════════════════════

    async def _block_heavy_media(self, page):
        """
        Block video/audio streams and large media to save bandwidth.
        YouTube page still loads fully (thumbnails, comments, UI) but the
        actual video stream (~1-2 GB/hr) never downloads.

        This keeps cookies, watch-history entries, and engagement signals
        while using almost zero extra data.
        """
        # Route handlers MUST be async def — lambdas returning coroutines
        # are NOT awaited by Playwright (it checks iscoroutinefunction).
        async def _abort_route(route):
            await route.abort()

        async def _filter_audio(route):
            if "googlevideo" in route.request.url:
                await route.abort()
            else:
                await route.continue_()

        try:
            await page.route("**/*.{mp4,webm,m4s,ts,m3u8,mpd}", _abort_route)
            await page.route("**/videoplayback*", _abort_route)
            await page.route("**/googlevideo.com/**", _abort_route)
            await page.route("**/audio*", _filter_audio)
            logger.debug("Media blocking enabled for bandwidth saving")
        except Exception as e:
            logger.debug(f"Media blocking setup error: {e}")

    async def watch_youtube(self, page, query: str):
        """
        Bandwidth-safe YouTube session:
        1. Block video/audio streams (saves ~1-2 GB/hr)
        2. Navigate to YouTube
        3. Search for persona-relevant query
        4. Click a video (page loads, video player shows but doesn't stream)
        5. Simulate watching for 30s–3min (scroll comments, move mouse)
        6. Maybe click a recommended video

        The profile still gets: cookies, watch history entries, search history,
        engagement signals, and all browsing fingerprint data — without
        downloading the actual video content.
        """
        _page_routes_added = False
        try:
            if not getattr(self, "_youtube_enabled", True):
                self._report("YouTube disabled — skipping")
                return
            self._report(f"Opening YouTube — searching for \"{query[:30]}\"...")
            # Block heavy media BEFORE navigating (skip if bandwidth_saver already blocks at context level)
            if not self._bandwidth_saver:
                await self._block_heavy_media(page)
                _page_routes_added = True

            ok = await self.safe_navigate(page, "https://www.youtube.com")
            if not ok:
                return

            await asyncio.sleep(random.uniform(1.5, 3.5))
            await self.dismiss_popups(page)

            # Type into YouTube search bar
            search_typed = False
            for selector in ['input#search', 'input[name="search_query"]']:
                search_typed = await self.type_like_human(page, selector, query)
                if search_typed:
                    break

            if not search_typed:
                # Fallback: direct URL
                search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
                await self.safe_navigate(page, search_url)
            else:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await page.keyboard.press("Enter")

            await asyncio.sleep(random.uniform(3, 6))
            await self.dismiss_popups(page)

            # Click a video from results
            video_selectors = [
                "ytd-video-renderer a#video-title",
                "a#video-title",
                "ytd-video-renderer h3 a",
                "#contents ytd-video-renderer a.yt-simple-endpoint",
            ]

            clicked_video = False
            for sel in video_selectors:
                try:
                    videos = await page.query_selector_all(sel)
                    visible = []
                    for v in videos[:10]:
                        try:
                            if await v.is_visible():
                                visible.append(v)
                        except Exception:
                            continue
                    if visible:
                        target = random.choice(visible[:5])
                        await target.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await target.hover()
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                        await target.click()
                        clicked_video = True
                        break
                except Exception:
                    continue

            if not clicked_video:
                logger.debug("YouTube: no video found to click")
                return

            # Watch the video
            await asyncio.sleep(random.uniform(2, 4))
            await self.dismiss_popups(page)

            watch_duration = random.uniform(20, 90)  # 20s to 1.5min
            elapsed = 0.0
            while elapsed < watch_duration:
                self._check_skip()
                activity = random.choices(
                    ["wait", "scroll_comments", "mouse", "hover_related"],
                    weights=[40, 25, 20, 15], k=1
                )[0]

                try:
                    if activity == "wait":
                        wait = random.uniform(3, 10)
                        await asyncio.sleep(wait)
                        elapsed += wait
                    elif activity == "scroll_comments":
                        await page.evaluate(f"window.scrollBy(0, {random.randint(300, 700)})")
                        wait = random.uniform(2, 5)
                        await asyncio.sleep(wait)
                        elapsed += wait
                    elif activity == "mouse":
                        await self.move_mouse_randomly(page)
                        wait = random.uniform(2, 5)
                        await asyncio.sleep(wait)
                        elapsed += wait
                    elif activity == "hover_related":
                        # Hover over recommended videos sidebar
                        try:
                            related = await page.query_selector_all(
                                "ytd-compact-video-renderer a, "
                                "ytd-rich-item-renderer a"
                            )
                            if related:
                                r = random.choice(related[:8])
                                await r.hover()
                        except Exception:
                            pass
                        wait = random.uniform(2, 4)
                        await asyncio.sleep(wait)
                        elapsed += wait
                except Exception:
                    elapsed += 5
                    await asyncio.sleep(5)

            # 40% chance: click a recommended video
            if random.random() < 0.4 and not page.is_closed():
                try:
                    related = await page.query_selector_all(
                        "ytd-compact-video-renderer a#thumbnail, "
                        "ytd-rich-item-renderer a#thumbnail"
                    )
                    visible_related = []
                    for r in related[:10]:
                        try:
                            if await r.is_visible():
                                visible_related.append(r)
                        except Exception:
                            continue
                    if visible_related:
                        pick = random.choice(visible_related[:5])
                        await pick.hover()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await pick.click()
                        # Watch briefly
                        await asyncio.sleep(random.uniform(10, 30))
                except Exception:
                    pass

            logger.debug(f"YouTube: watched ~{int(watch_duration)}s for '{query[:30]}'")

        except Exception as e:
            logger.debug(f"YouTube watch error: {e}")
        finally:
            # Remove page-level media blocking so other sites work normally
            # (only if we added them — skip if bandwidth_saver handles it at context level)
            if _page_routes_added:
                try:
                    await page.unroute("**/*.{mp4,webm,m4s,ts,m3u8,mpd}")
                    await page.unroute("**/videoplayback*")
                    await page.unroute("**/googlevideo.com/**")
                    await page.unroute("**/audio*")
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════
    #  ADDRESS BAR NAVIGATION  (#7)
    # ══════════════════════════════════════════════════════════════

    async def navigate_via_address_bar(self, page, url: str) -> bool:
        """
        Navigate by typing into the address bar (Ctrl+L → type → Enter).
        Generates more realistic browser-level events than page.goto().
        """
        try:
            domain = urlparse(url).netloc or url[:40]
            self._report(f"Navigating via address bar — typing {domain}...")
            # Focus the address bar
            await page.keyboard.down("Control")
            await page.keyboard.press("l")
            await page.keyboard.up("Control")
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Type the URL — humans often type partial URLs for known sites
            # e.g., "youtube.com" not "https://www.youtube.com"
            short_url = url
            if random.random() < 0.6:
                short_url = url.replace("https://www.", "").replace("https://", "").replace("http://www.", "").replace("http://", "")
                # Sometimes only type the domain
                if "/" in short_url and random.random() < 0.3:
                    short_url = short_url.split("/")[0]

            # Type it with realistic speed (faster than form typing — muscle memory)
            delay_min = self.timing.get("typing_delay_min_ms", 80) * 0.6
            delay_max = self.timing.get("typing_delay_max_ms", 280) * 0.5
            for char in short_url:
                await page.keyboard.type(char, delay=random.uniform(delay_min, delay_max))

            await asyncio.sleep(random.uniform(0.3, 1.0))
            await page.keyboard.press("Enter")

            # Wait for navigation
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            
            # Bring tab to front so user can see
            await self._ensure_tab_visible(page)
            await asyncio.sleep(random.uniform(1.5, 4))

            # Handle obstacles
            await self.handle_cloudflare(page)
            await self.dismiss_popups(page)

            if await self.is_dead_page(page):
                self._report("Page not found — skipping")
                return False

            ok = not page.is_closed()
            if ok and self._nav_success_cb:
                try:
                    self._nav_success_cb()
                except Exception:
                    pass
            return ok

        except Exception as e:
            logger.debug(f"Address bar navigation error: {e}")
            if self._is_network_error(e):
                self._fire_network_error(url, e)
            return False

    # ══════════════════════════════════════════════════════════════
    #  FORM INTERACTION (non-auth)  (#8)
    # ══════════════════════════════════════════════════════════════

    async def interact_with_page_forms(self, page):
        """
        Interact with non-auth form elements on the page:
        - Click dropdown <select> elements and pick an option
        - Toggle checkboxes (filters)
        - Use site-internal search boxes
        - Click sortable table headers
        """
        self._check_stop()  # Hard stop check
        if page.is_closed() or self._should_skip_auth_ui(page):
            return
        self._report("Interacting with page — dropdowns, filters, search boxes...")
        interactions_done = 0

        try:
            # ── Select dropdowns (sort, filter, etc.) ──
            if random.random() < 0.5:
                selects = await page.query_selector_all("select")
                for sel in selects[:5]:
                    try:
                        if not await sel.is_visible():
                            continue
                        # Skip login/auth related selects
                        name = (await sel.get_attribute("name") or "").lower()
                        sel_id = (await sel.get_attribute("id") or "").lower()
                        if any(kw in name + sel_id for kw in ["password", "login", "auth", "country", "state"]):
                            continue

                        options = await sel.query_selector_all("option")
                        if len(options) > 1:
                            # Pick a random non-first option
                            pick = random.choice(options[1:min(5, len(options))])
                            value = await pick.get_attribute("value")
                            if value:
                                await sel.scroll_into_view_if_needed()
                                await asyncio.sleep(random.uniform(0.3, 0.8))
                                await sel.select_option(value=value)
                                await asyncio.sleep(random.uniform(1, 3))
                                interactions_done += 1
                                break  # One dropdown per visit
                    except Exception:
                        continue

            # ── Filter checkboxes ──
            if random.random() < 0.35:
                checkboxes = await page.query_selector_all(
                    "input[type='checkbox']:not([name*='agree']):not([name*='terms'])"
                    ":not([name*='newsletter']):not([name*='subscribe'])"
                )
                for cb in checkboxes[:8]:
                    try:
                        if not await cb.is_visible():
                            continue
                        cb_id = (await cb.get_attribute("id") or "").lower()
                        cb_name = (await cb.get_attribute("name") or "").lower()
                        # Skip anything auth/marketing related
                        if any(kw in cb_id + cb_name for kw in [
                            "agree", "terms", "newsletter", "subscribe", "consent",
                            "remember", "login", "password"
                        ]):
                            continue
                        await cb.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await cb.click()
                        await asyncio.sleep(random.uniform(0.5, 2))
                        interactions_done += 1
                        if interactions_done >= 2:
                            break
                    except Exception:
                        continue

            # ── Site-internal search box ──
            if random.random() < 0.3:
                search_selectors = [
                    "input[type='search']",
                    "input[placeholder*='earch']",
                    "input[aria-label*='earch']",
                    "[role='search'] input",
                ]
                for sel in search_selectors:
                    try:
                        box = await page.query_selector(sel)
                        if box and await box.is_visible():
                            # Skip if it's the main Google search bar
                            name = (await box.get_attribute("name") or "").lower()
                            if name in ("q", "search_query"):
                                continue
                            # Type a short generic term
                            terms = ["help", "guide", "docs", "pricing", "free",
                                     "popular", "best", "new", "how to", "tutorial"]
                            await box.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            await box.click()
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            term = random.choice(terms)
                            await page.keyboard.type(term, delay=random.uniform(80, 220))
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(random.uniform(1.5, 3.5))
                            interactions_done += 1
                            break
                    except Exception:
                        continue

            # ── Sortable table/list headers ──
            if random.random() < 0.25:
                sort_links = await page.query_selector_all(
                    "[data-sort], th[role='columnheader'], "
                    "a[href*='sort'], button[aria-label*='ort']"
                )
                visible_sorts = []
                for sl in sort_links[:8]:
                    try:
                        if await sl.is_visible():
                            visible_sorts.append(sl)
                    except Exception:
                        continue
                if visible_sorts:
                    pick = random.choice(visible_sorts[:4])
                    await pick.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await pick.click()
                    await asyncio.sleep(random.uniform(1.5, 4))
                    interactions_done += 1

        except Exception as e:
            logger.debug(f"Form interaction error: {e}")

        return interactions_done > 0

    # ══════════════════════════════════════════════════════════════
    #  DEEP SITE ENGAGEMENT (type, write, click login/CTA)
    # ══════════════════════════════════════════════════════════════

    # Phrases a real human might type into various input fields
    _ENGAGEMENT_PHRASES = {
        "search": [
            "how to get started", "pricing plans", "free trial",
            "documentation", "contact support", "latest updates",
            "best features", "getting started guide", "help center",
            "compare plans", "what's new", "tutorial", "demo",
        ],
        "email": [
            "test{}@gmail.com", "user{}@outlook.com",
            "myemail{}@yahoo.com", "hello{}@protonmail.com",
        ],
        "name": [
            "John", "Sarah", "Mike", "Emma", "Alex", "Chris",
            "David", "Lisa", "James", "Anna", "Tom", "Kate",
        ],
        "message": [
            "Hi, I'm interested in learning more about your product.",
            "Could you send me more information about pricing?",
            "I'd like to request a demo, please.",
            "Looking for more details on your services.",
            "I have a question about your free tier.",
        ],
        "comment": [
            "Great article, thanks for sharing!",
            "This is really helpful, bookmarking for later.",
            "Interesting perspective on this topic.",
            "Nice writeup, very informative.",
            "Thanks for the detailed explanation.",
        ],
        "username": [
            "cooluser{}", "browserman{}", "webfan{}",
            "techuser{}", "viewer{}", "reader{}",
        ],
    }

    async def engage_with_site_ui(self, page):
        """
        Advanced site engagement — simulates a real human interacting with
        the site before leaving. Randomly picks several actions:

        - Type into visible text inputs / textareas (names, messages, comments)
        - Type into search boxes with site-relevant queries
        - Click sign-in / login / sign-up buttons (just the click, not full auth)
        - Click CTA buttons (Get Started, Learn More, Try Free, etc.)
        - Expand nav menus / hamburger menus
        - Click newsletter subscribe (without submitting email)
        - Interact with tabs, accordions, carousels

        All actions are wrapped in try/except so failures are silent.
        The method never submits real forms or creates accounts.
        """
        if page.is_closed():
            return
        if self._should_skip_auth_ui(page):
            return

        self._report("Engaging with UI — typing in fields, clicking buttons...")
        actions_done = 0
        rng_suffix = str(random.randint(100, 9999))

        try:
            # ── 1. Type into visible text inputs ──────────────────
            if random.random() < 0.40:
                inputs = await page.query_selector_all(
                    "input[type='text'], input[type='email'], "
                    "input[type='search'], input:not([type]), textarea"
                )
                for inp in inputs[:8]:
                    try:
                        if not await inp.is_visible():
                            continue

                        inp_type = (await inp.get_attribute("type") or "").lower()
                        inp_name = (await inp.get_attribute("name") or "").lower()
                        inp_ph = (await inp.get_attribute("placeholder") or "").lower()
                        inp_id = (await inp.get_attribute("id") or "").lower()
                        combined = inp_name + inp_ph + inp_id + inp_type

                        # Skip password / hidden / CAPTCHA fields
                        if any(kw in combined for kw in [
                            "password", "captcha", "hidden", "token", "csrf",
                            "card", "cvv", "ssn", "credit",
                        ]):
                            continue

                        # Determine what to type based on field semantics
                        text_to_type = ""
                        if any(kw in combined for kw in ["email", "e-mail", "mail"]):
                            tpl = random.choice(self._ENGAGEMENT_PHRASES["email"])
                            text_to_type = tpl.format(rng_suffix)
                        elif any(kw in combined for kw in [
                            "name", "first", "last", "full", "user",
                        ]):
                            if "user" in combined:
                                tpl = random.choice(self._ENGAGEMENT_PHRASES["username"])
                                text_to_type = tpl.format(rng_suffix)
                            else:
                                text_to_type = random.choice(self._ENGAGEMENT_PHRASES["name"])
                        elif any(kw in combined for kw in [
                            "search", "query", "find", "lookup",
                        ]):
                            text_to_type = random.choice(self._ENGAGEMENT_PHRASES["search"])
                        elif any(kw in combined for kw in [
                            "message", "comment", "feedback", "description",
                            "note", "text", "body", "content",
                        ]):
                            text_to_type = random.choice(self._ENGAGEMENT_PHRASES["comment"])
                        elif inp_type == "email" or "email" in combined:
                            tpl = random.choice(self._ENGAGEMENT_PHRASES["email"])
                            text_to_type = tpl.format(rng_suffix)
                        else:
                            # Generic: short phrase
                            text_to_type = random.choice(
                                self._ENGAGEMENT_PHRASES["search"]
                            )

                        if not text_to_type:
                            continue

                        await inp.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.4, 1.2))
                        await inp.click()
                        await asyncio.sleep(random.uniform(0.3, 0.8))

                        # Clear any existing value
                        await page.keyboard.down("Control")
                        await page.keyboard.press("a")
                        await page.keyboard.up("Control")
                        await asyncio.sleep(random.uniform(0.1, 0.3))

                        # Type with realistic human speed
                        speed = random.uniform(80, 240)
                        await page.keyboard.type(text_to_type, delay=speed)
                        await asyncio.sleep(random.uniform(0.8, 2.5))
                        actions_done += 1

                        # Only fill 1-2 fields per visit
                        if actions_done >= random.randint(1, 2):
                            break
                    except Exception:
                        continue

            # ── 2. Type into textareas (comments, messages) ────────
            if random.random() < 0.30 and actions_done < 3:
                textareas = await page.query_selector_all("textarea")
                for ta in textareas[:4]:
                    try:
                        if not await ta.is_visible():
                            continue

                        ta_name = (await ta.get_attribute("name") or "").lower()
                        ta_ph = (await ta.get_attribute("placeholder") or "").lower()
                        ta_id = (await ta.get_attribute("id") or "").lower()
                        combined = ta_name + ta_ph + ta_id

                        if any(kw in combined for kw in ["password", "captcha", "hidden"]):
                            continue

                        if any(kw in combined for kw in ["comment", "message", "feedback", "reply"]):
                            text = random.choice(self._ENGAGEMENT_PHRASES["comment"])
                        else:
                            text = random.choice(self._ENGAGEMENT_PHRASES["message"])

                        await ta.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await ta.click()
                        await asyncio.sleep(random.uniform(0.3, 0.8))

                        speed = random.uniform(85, 250)
                        await page.keyboard.type(text, delay=speed)
                        await asyncio.sleep(random.uniform(1.0, 3.0))
                        actions_done += 1
                        break  # One textarea is enough
                    except Exception:
                        continue

            # ── 3. Click login / sign-in / sign-up buttons ────────
            if random.random() < 0.35:
                auth_selectors = [
                    "a[href*='login']", "a[href*='signin']", "a[href*='sign-in']",
                    "a[href*='signup']", "a[href*='sign-up']", "a[href*='register']",
                    "button:has-text('Log in')", "button:has-text('Sign in')",
                    "button:has-text('Sign up')", "button:has-text('Register')",
                    "button:has-text('Login')", "button:has-text('Get started')",
                    "a:has-text('Log in')", "a:has-text('Sign in')",
                    "a:has-text('Sign up')", "a:has-text('Get started')",
                    "a:has-text('Create account')", "a:has-text('Join')",
                    "[data-testid*='login']", "[data-testid*='signin']",
                ]
                for sel in auth_selectors:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            self._report("Clicking sign-in button...")
                            await btn.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            await self.move_mouse_randomly(page)
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await btn.hover()
                            await asyncio.sleep(random.uniform(0.2, 0.6))
                            await btn.click()
                            actions_done += 1

                            # Wait for the login page / modal to load
                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded", timeout=8000
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(random.uniform(2.0, 5.0))

                            # On the login/signup page: interact with its fields too
                            await self.dismiss_popups(page)
                            login_fields = await page.query_selector_all(
                                "input[type='email'], input[type='text'], "
                                "input[name*='email'], input[name*='user'], "
                                "input[placeholder*='mail'], input[placeholder*='user']"
                            )
                            for field in login_fields[:2]:
                                try:
                                    if not await field.is_visible():
                                        continue
                                    ftype = (await field.get_attribute("type") or "").lower()
                                    if ftype == "password":
                                        continue
                                    fname = (await field.get_attribute("name") or "").lower()
                                    fph = (await field.get_attribute("placeholder") or "").lower()

                                    if "email" in fname + fph or ftype == "email":
                                        tpl = random.choice(self._ENGAGEMENT_PHRASES["email"])
                                        val = tpl.format(rng_suffix)
                                    else:
                                        tpl = random.choice(self._ENGAGEMENT_PHRASES["username"])
                                        val = tpl.format(rng_suffix)

                                    await field.scroll_into_view_if_needed()
                                    await asyncio.sleep(random.uniform(0.3, 0.8))
                                    await field.click()
                                    await asyncio.sleep(random.uniform(0.2, 0.5))
                                    speed = random.uniform(85, 230)
                                    await page.keyboard.type(val, delay=speed)
                                    await asyncio.sleep(random.uniform(0.8, 2.0))
                                    actions_done += 1
                                    break  # One field is enough on login page
                                except Exception:
                                    continue

                            # Brief dwell on login page, then go back
                            await asyncio.sleep(random.uniform(1.5, 4.0))
                            await self.move_mouse_randomly(page)

                            # Go back to the original page
                            try:
                                await page.go_back()
                                await asyncio.sleep(random.uniform(1.5, 3.0))
                            except Exception:
                                pass
                            break  # Only click one auth button
                    except Exception:
                        continue

            # ── 4. Click CTA buttons (Get Started, Learn More, etc.) ──
            if random.random() < 0.30 and actions_done < 4:
                cta_selectors = [
                    "a:has-text('Learn more')", "a:has-text('Read more')",
                    "a:has-text('Try free')", "a:has-text('Try it free')",
                    "a:has-text('Start free')", "a:has-text('Get started')",
                    "a:has-text('See pricing')", "a:has-text('View plans')",
                    "a:has-text('Explore')", "a:has-text('Discover')",
                    "button:has-text('Learn more')", "button:has-text('Try free')",
                    "button:has-text('Get started')", "button:has-text('Explore')",
                    "[role='button']:has-text('Learn')",
                ]
                random.shuffle(cta_selectors)
                for sel in cta_selectors[:5]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            self._report("Clicking CTA button...")
                            await btn.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(0.5, 1.2))
                            await btn.hover()
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await btn.click()
                            actions_done += 1

                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded", timeout=8000
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(random.uniform(2.0, 5.0))
                            await self.dismiss_popups(page)
                            # Scroll the new page briefly
                            await self.scroll_page(page)
                            await asyncio.sleep(random.uniform(1.0, 3.0))
                            break
                    except Exception:
                        continue

            # ── 5. Expand nav menus / hamburger buttons ────────────
            if random.random() < 0.20 and actions_done < 5:
                nav_selectors = [
                    "button[aria-label*='enu']", "button[aria-label*='nav']",
                    "[class*='hamburger']", "[class*='menu-toggle']",
                    "[class*='navbar-toggler']", "button[class*='menu']",
                    "details summary",
                ]
                for sel in nav_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await el.click()
                            await asyncio.sleep(random.uniform(1.0, 3.0))
                            # Move mouse around the opened menu
                            await self.move_mouse_randomly(page)
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            actions_done += 1
                            break
                    except Exception:
                        continue

            # ── 6. Click accordion / tab elements ──────────────────
            if random.random() < 0.25 and actions_done < 5:
                accordion_selectors = [
                    "[role='tab']", "[data-toggle='tab']",
                    "[data-bs-toggle='tab']", "[data-toggle='collapse']",
                    "[data-bs-toggle='collapse']",
                    "details:not([open]) summary",
                    "[class*='accordion'] button", "[class*='faq'] button",
                ]
                for sel in accordion_selectors:
                    try:
                        items = await page.query_selector_all(sel)
                        visible = []
                        for item in items[:6]:
                            try:
                                if await item.is_visible():
                                    visible.append(item)
                            except Exception:
                                continue
                        if visible:
                            pick = random.choice(visible[:4])
                            await pick.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await pick.click()
                            await asyncio.sleep(random.uniform(1.5, 4.0))
                            actions_done += 1
                            break
                    except Exception:
                        continue

        except Exception as e:
            logger.debug(f"Site UI engagement error: {e}")

        if actions_done > 0:
            self._report(f"Engaged with {actions_done} UI elements")
        return actions_done > 0

    # ══════════════════════════════════════════════════════════════
    #  TAB CLOSING  (#2)
    # ══════════════════════════════════════════════════════════════

    async def close_random_tab(self, context, keep_minimum: int = 1) -> bool:
        """
        Close a random tab that we're done with (natural tab lifecycle).
        Never closes the last tab. Returns True if a tab was closed.
        """
        try:
            alive = [p for p in context.pages if not p.is_closed()]
            if len(alive) <= keep_minimum:
                return False

            # Pick a tab to close (prefer older tabs / tabs not in front)
            # The last tab in the list is usually the most recent
            candidates = alive[:-1] if len(alive) > 1 else alive
            if not candidates:
                return False

            victim = random.choice(candidates)
            await victim.close()
            logger.debug(f"Closed tab (remaining: {len(alive) - 1})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            return True

        except Exception as e:
            logger.debug(f"Tab close error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    #  REFERRER CHAIN BUILDING  (#9)
    # ══════════════════════════════════════════════════════════════

    async def build_referrer_chain(self, page, context, start_url: str,
                                    target_url: str) -> bool:
        """
        Navigate to target_url via an intermediate site (referrer chain).

        Chains:
        1. Google → target  (search for something related, click result)
        2. Reddit/HN → target  (visit aggregator, find link to target)
        3. Direct link hop  (visit start_url, find link to target domain)
        """
        self._check_stop()  # Hard stop check
        target_domain = urlparse(target_url).netloc.replace("www.", "")

        try:
            # Strategy 1: Go through start_url, try to find a link to target
            ok = await self.safe_navigate(page, start_url)
            if not ok:
                return False

            await asyncio.sleep(random.uniform(1.5, 3.5))
            await self.dismiss_popups(page)
            await self.scroll_page(page)

            # Look for links to the target domain
            links = await page.query_selector_all("a[href]")
            for link in links[:30]:
                try:
                    href = await link.get_attribute("href") or ""
                    if target_domain in href.lower() and await link.is_visible():
                        await link.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await link.hover()
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                        await link.click()
                        await asyncio.sleep(random.uniform(1.5, 4.0))
                        await self.dismiss_popups(page)
                        logger.debug(f"Referrer chain: {urlparse(start_url).netloc} → {target_domain}")
                        return True
                except Exception:
                    continue

            # If no link found on start_url, just navigate directly
            return False

        except Exception as e:
            logger.debug(f"Referrer chain error: {e}")
            return False


    # ══════════════════════════════════════════════════════════════
    #  PAGE OBSERVER — classify from DOM, then act
    # ══════════════════════════════════════════════════════════════

    _PAGE_OBSERVER_JS = r"""() => {
      const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width >= 8 && r.height >= 8
          && st.visibility !== 'hidden' && st.display !== 'none'
          && parseFloat(st.opacity || '1') > 0.05
          && r.bottom > 0 && r.top < (window.innerHeight * 4);
      };
      const textOf = (el) => ((el.innerText || el.getAttribute('aria-label') || '') + '').trim();
      const sameHost = (href) => {
        try { return new URL(href, location.href).origin === location.origin; }
        catch (e) { return false; }
      };
      const checkoutRe = /checkout|pay now|place order|оплат|paypal|stripe/i;
      const cartBtnRe = /add to (cart|bag|basket)|add-to-cart|добавить в/i;
      const skipLinkRe = /login|signup|sign-up|register|checkout|privacy|terms|cookie|javascript:|^#/i;

      let cartButtons = 0;
      let miniCart = 0;
      for (const el of document.querySelectorAll('button, a, [role="button"], input[type="submit"]')) {
        if (!vis(el)) continue;
        const t = textOf(el) + ' ' + (el.getAttribute('aria-label') || '');
        if (checkoutRe.test(t)) continue;
        if (cartBtnRe.test(t)) cartButtons++;
        if (/mini[- ]?cart|shopping[- ]?(cart|bag)|корзин/i.test(t + ' ' + (el.getAttribute('class') || ''))) {
          miniCart++;
        }
      }

      const roots = [
        document.querySelector('main'),
        document.querySelector('[role="main"]'),
        document.body,
      ].filter(Boolean);

      const findCards = (root) => {
        const byParent = new Map();
        for (const el of root.querySelectorAll('div, li, article, a, section')) {
          if (!vis(el)) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 72 || r.height < 72 || r.width > window.innerWidth * 0.92) continue;
          const parent = el.parentElement;
          if (!parent) continue;
          if (!byParent.has(parent)) byParent.set(parent, []);
          byParent.get(parent).push(el);
        }
        let best = [];
        for (const els of byParent.values()) {
          if (els.length < 4) continue;
          const areas = els.map((e) => {
            const r = e.getBoundingClientRect();
            return r.width * r.height;
          }).sort((a, b) => a - b);
          const median = areas[Math.floor(areas.length / 2)] || 1;
          const similar = els.filter((e) => {
            const r = e.getBoundingClientRect();
            const a = r.width * r.height;
            return Math.abs(a - median) / median < 0.55;
          });
          const withImg = similar.filter((e) =>
            e.querySelector('img, picture, [style*="background-image"]'));
          const withText = withImg.filter((e) => {
            const t = textOf(e);
            return t.length >= 2 && t.length < 500;
          });
          if (withText.length >= 4 && withText.length > best.length) best = withText;
        }
        return best;
      };

      let cards = [];
      for (const root of roots) {
        const g = findCards(root);
        if (g.length > cards.length) cards = g;
      }

      const cardHrefs = [];
      const seenCards = new Set();
      cards.slice(0, 24).forEach((el, i) => {
        el.setAttribute('data-warmup-card', String(i));
        const a = el.closest('a[href]') || el.querySelector('a[href]');
        const href = a && a.href ? a.href : '';
        if (href && sameHost(href) && !skipLinkRe.test(href)) {
          const key = href.split('#')[0].split('?')[0];
          if (!seenCards.has(key)) {
            seenCards.add(key);
            cardHrefs.push(href);
          }
        }
      });

      const asides = document.querySelectorAll(
        'aside, nav, [role="navigation"], [class*="sidebar" i], [class*="toc" i], [class*="menu" i]'
      );
      let sidebarHrefs = [];
      for (const box of asides) {
        if (!vis(box)) continue;
        const links = [...box.querySelectorAll('a[href]')].filter(vis);
        const same = links.filter((a) => sameHost(a.href) && !skipLinkRe.test(a.href + ' ' + textOf(a)));
        if (same.length >= 5 && same.length > sidebarHrefs.length) {
          sidebarHrefs = [...new Set(same.map((a) => a.href))];
        }
      }

      const headings = document.querySelectorAll('h1, h2, h3').length;
      const codeBlocks = document.querySelectorAll('pre, code').length;
      const article = document.querySelector(
        'article, [class*="prose" i], .markdown, .mdx-content, main'
      );
      const articleLen = article ? textOf(article).length : 0;

      let searchLike = 0;
      let playground = 0;
      for (const el of document.querySelectorAll('input, textarea, [contenteditable="true"]')) {
        if (!vis(el)) continue;
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (type === 'password' || type === 'email' || type === 'hidden') continue;
        const ph = [
          el.getAttribute('placeholder') || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('name') || '',
        ].join(' ');
        if (/login|password|email|username/i.test(ph)) continue;
        const r = el.getBoundingClientRect();
        if (type === 'search' || /search|find|фильтр|поиск/i.test(ph)) searchLike++;
        if ((el.tagName === 'TEXTAREA' || el.getAttribute('contenteditable'))
            && r.height > 60) playground++;
      }

      const main = document.querySelector('main, [role="main"]') || document.body;
      const promo = [];
      const seenPromo = new Set();
      for (const a of main.querySelectorAll('a[href]')) {
        if (!vis(a) || !sameHost(a.href)) continue;
        const t = textOf(a);
        if (skipLinkRe.test(a.href + ' ' + t)) continue;
        const r = a.getBoundingClientRect();
        if (r.width < 40 || r.height < 12) continue;
        if (a.closest('footer, [role="contentinfo"]')) continue;
        const key = a.href.split('#')[0].split('?')[0];
        if (seenPromo.has(key)) continue;
        seenPromo.add(key);
        promo.push({ href: a.href, area: r.width * r.height, y: r.top, text: t.slice(0, 80) });
      }
      promo.sort((a, b) => b.area - a.area || a.y - b.y);

      let mode = 'explorer';
      const reasons = [];
      if (cards.length >= 4 || (cards.length >= 2 && cartButtons >= 1)) {
        mode = 'shopper';
        if (cards.length) reasons.push(cards.length + ' similar cards');
        if (cartButtons) reasons.push('cart button');
        if (miniCart) reasons.push('mini-cart');
      } else if (
        sidebarHrefs.length >= 5
        || (headings >= 4 && codeBlocks >= 3)
        || (articleLen > 1500 && sidebarHrefs.length >= 3)
      ) {
        mode = 'docs_reader';
        if (sidebarHrefs.length) reasons.push(sidebarHrefs.length + ' sidebar links');
        if (codeBlocks) reasons.push(codeBlocks + ' code blocks');
        if (headings) reasons.push(headings + ' headings');
      } else {
        reasons.push('prominent in-page links');
      }

      return {
        mode,
        reason: reasons.join(', ') || 'default',
        cardCount: cards.length,
        cardHrefs: cardHrefs.slice(0, 20),
        cartButtons,
        miniCart,
        sidebarHrefs: sidebarHrefs.slice(0, 20),
        headings,
        codeBlocks,
        articleLen,
        searchLike,
        playground,
        promoHrefs: promo.map((p) => p.href).slice(0, 20),
      };
    }"""

    _EMPTY_OBSERVATION = {
        "mode": "explorer",
        "reason": "unreadable",
        "cardCount": 0,
        "cardHrefs": [],
        "cartButtons": 0,
        "miniCart": 0,
        "sidebarHrefs": [],
        "headings": 0,
        "codeBlocks": 0,
        "articleLen": 0,
        "searchLike": 0,
        "playground": 0,
        "promoHrefs": [],
    }

    def _norm_page_key(self, url: str) -> str:
        try:
            return (url or "").split("?")[0].split("#")[0].rstrip("/").lower()
        except Exception:
            return (url or "").lower()

    async def _observe_page(self, page) -> dict:
        """One DOM pass: what is on this page right now."""
        obs = dict(self._EMPTY_OBSERVATION)
        if page is None or page.is_closed():
            return obs
        try:
            data = await page.evaluate(self._PAGE_OBSERVER_JS)
            if isinstance(data, dict):
                obs.update(data)
                if obs.get("mode") not in ("shopper", "docs_reader", "explorer"):
                    obs["mode"] = "explorer"
        except Exception as e:
            logger.debug(f"page observe failed: {e}")
        return obs

    async def _report_page_mode(self, obs: dict):
        reason = obs.get("reason") or "default"
        self._report(f"Page mode: {obs.get('mode', 'explorer')} ({reason})")

    async def _open_observed_href(self, page, href: str, visited: set) -> bool:
        key = self._norm_page_key(href)
        if not href or key in visited:
            return False
        ok = await self.safe_navigate(page, href)
        if not ok or page.is_closed():
            return False
        visited.add(self._norm_page_key(page.url) or key)
        await self._wait_full_page_load(page)
        await self.dismiss_popups(page)
        return True

    async def _type_onsite_search(self, page, terms: list) -> bool:
        """Type into a visible search box. Never login/password fields."""
        if page.is_closed() or not terms:
            return False
        query = random.choice([t for t in terms if t and str(t).strip()] or [])
        if not query:
            return False
        query = str(query).strip()[:80]
        selectors = [
            "input[type='search']",
            "input[placeholder*='earch' i]",
            "input[aria-label*='earch' i]",
            "input[name*='earch' i]",
            "[role='search'] input",
            "input[placeholder*='оиск']",
        ]
        for sel in selectors:
            try:
                box = await page.query_selector(sel)
                if not box or not await box.is_visible():
                    continue
                name = (await box.get_attribute("name") or "").lower()
                typ = (await box.get_attribute("type") or "").lower()
                host = ""
                try:
                    host = urlparse(page.url).netloc.lower()
                except Exception:
                    pass
                if typ in ("password", "email"):
                    continue
                if name in ("q", "search_query") and "google." in host:
                    continue
                await box.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await box.click()
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await page.keyboard.type(query, delay=random.uniform(70, 180))
                await asyncio.sleep(random.uniform(0.4, 1.0))
                await page.keyboard.press("Enter")
                self._report(f"On-site search: {query}")
                await self._wait_full_page_load(page)
                await self.dismiss_popups(page)
                return True
            except Exception:
                continue
        return False

    async def _click_warmup_card(self, page, index: int) -> bool:
        try:
            el = await page.query_selector(f'[data-warmup-card="{index}"]')
            if not el or not await el.is_visible():
                return False
            await el.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await el.click(timeout=4000)
            await self._wait_full_page_load(page)
            await self.dismiss_popups(page)
            return True
        except Exception:
            return False

    async def _click_visible_next(self, page) -> bool:
        """Paginate if a visible Next / Load more control exists."""
        if page.is_closed():
            return False
        try:
            clicked = await page.evaluate(
                r"""() => {
                  const re = /^(next|older|load more|show more|далее|ещё|еще|>)$/i;
                  const nodes = document.querySelectorAll('a, button, [role="button"]');
                  for (const el of nodes) {
                    const t = ((el.innerText || el.getAttribute('aria-label') || '') + '').trim();
                    if (!re.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) continue;
                    el.click();
                    return true;
                  }
                  return false;
                }"""
            )
            if clicked:
                await self._wait_full_page_load(page)
                await self.dismiss_popups(page)
                return True
        except Exception:
            pass
        return False

    async def _dwell_current_page(self, page, metrics=None):
        if page.is_closed():
            return
        await self.simulate_reading(page)
        if metrics:
            try:
                metrics.record_page_visit(page.url)
            except Exception:
                pass

    async def _act_observed_page(
        self, page, context, obs: dict, *,
        visited: set, remaining: int,
        item_terms=None, metrics=None, target_domain: str = "",
    ) -> int:
        """Act in the current page mode. Returns how many new pages were opened."""
        _ = context
        if remaining <= 0 or page.is_closed():
            return 0
        mode = obs.get("mode") or "explorer"
        opened = 0
        item_terms = [t for t in (item_terms or []) if t]

        await self._dwell_current_page(page, metrics=None)

        if mode == "shopper" and obs.get("searchLike") and item_terms and random.random() < 0.45:
            if await self._type_onsite_search(page, item_terms):
                opened += 1
                obs = await self._observe_page(page)
                await self._report_page_mode(obs)
                mode = obs.get("mode") or mode

        if mode == "docs_reader" and obs.get("searchLike") and item_terms and random.random() < 0.5:
            if await self._type_onsite_search(page, item_terms):
                opened += 1
                obs = await self._observe_page(page)

        if mode == "shopper":
            hrefs = [h for h in (obs.get("cardHrefs") or []) if self._norm_page_key(h) not in visited]
            n = min(remaining - opened, random.randint(3, 6), max(1, len(hrefs) or obs.get("cardCount") or 0))
            if hrefs:
                random.shuffle(hrefs)
                for href in hrefs[:n]:
                    self._check_skip()
                    if page.is_closed() or opened >= remaining:
                        break
                    origin = page.url
                    if not await self._open_observed_href(page, href, visited):
                        continue
                    opened += 1
                    if metrics:
                        try:
                            metrics.record_link_click()
                            metrics.record_page_visit(page.url)
                        except Exception:
                            pass
                    self._report("Shopper — opened an item")
                    await self._dwell_current_page(page)
                    inner = await self._observe_page(page)
                    if inner.get("cartButtons"):
                        try:
                            await self.maybe_shop_cart(page)
                        except (_StopRequested, _SkipPhase):
                            raise
                        except Exception:
                            pass
                    try:
                        if not page.is_closed() and page.url != origin:
                            await page.go_back(wait_until="domcontentloaded", timeout=8000)
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                    except Exception:
                        pass
            elif (obs.get("cardCount") or 0) > 0:
                for idx in range(min(n, int(obs.get("cardCount") or 0), 8)):
                    self._check_skip()
                    if page.is_closed() or opened >= remaining:
                        break
                    origin = page.url
                    if not await self._click_warmup_card(page, idx):
                        continue
                    key = self._norm_page_key(page.url)
                    if key in visited:
                        continue
                    visited.add(key)
                    opened += 1
                    if metrics:
                        try:
                            metrics.record_link_click()
                            metrics.record_page_visit(page.url)
                        except Exception:
                            pass
                    self._report("Shopper — opened a card")
                    await self._dwell_current_page(page)
                    inner = await self._observe_page(page)
                    if inner.get("cartButtons"):
                        try:
                            await self.maybe_shop_cart(page)
                        except (_StopRequested, _SkipPhase):
                            raise
                        except Exception:
                            pass
                    try:
                        if not page.is_closed() and page.url != origin:
                            await page.go_back(wait_until="domcontentloaded", timeout=8000)
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                    except Exception:
                        pass
            if opened < remaining and random.random() < 0.4:
                if await self._click_visible_next(page):
                    visited.add(self._norm_page_key(page.url))
                    opened += 1
            return opened

        if mode == "docs_reader":
            hrefs = [h for h in (obs.get("sidebarHrefs") or obs.get("promoHrefs") or [])
                     if self._norm_page_key(h) not in visited]
            random.shuffle(hrefs)
            n = min(remaining - opened, random.randint(3, 5), max(1, len(hrefs)))
            for href in hrefs[:n]:
                self._check_skip()
                if page.is_closed() or opened >= remaining:
                    break
                if not await self._open_observed_href(page, href, visited):
                    continue
                opened += 1
                if metrics:
                    try:
                        metrics.record_link_click()
                        metrics.record_page_visit(page.url)
                    except Exception:
                        pass
                self._report("Reading — following on-page docs/nav")
                await self._dwell_current_page(page)
            return opened

        hrefs = [h for h in (obs.get("promoHrefs") or []) if self._norm_page_key(h) not in visited]
        random.shuffle(hrefs)
        n = min(remaining - opened, random.randint(2, 4), max(1, len(hrefs)))
        for href in hrefs[:n]:
            self._check_skip()
            if page.is_closed() or opened >= remaining:
                break
            if not await self._open_observed_href(page, href, visited):
                continue
            opened += 1
            if metrics:
                try:
                    metrics.record_link_click()
                    metrics.record_page_visit(page.url)
                except Exception:
                    pass
            self._report("Explorer — opened a page link")
            await self._dwell_current_page(page)
            nxt = await self._observe_page(page)
            await self._report_page_mode(nxt)
            if nxt.get("mode") == "shopper" and opened < remaining:
                extra = await self._act_observed_page(
                    page, context, nxt, visited=visited,
                    remaining=remaining - opened,
                    item_terms=item_terms, metrics=metrics,
                    target_domain=target_domain,
                )
                opened += extra
                break
            if nxt.get("mode") == "docs_reader" and opened < remaining:
                extra = await self._act_observed_page(
                    page, context, nxt, visited=visited,
                    remaining=remaining - opened,
                    item_terms=item_terms, metrics=metrics,
                    target_domain=target_domain,
                )
                opened += extra
                break
        return opened

    async def _explore_observed_site(
        self, page, context, target_domain: str,
        max_pages: int = 8, item_terms=None, metrics=None,
    ):
        """Landed on a site: observe, act, re-classify after each hop."""
        visited = set()
        try:
            visited.add(self._norm_page_key(page.url))
        except Exception:
            pass
        if metrics:
            try:
                metrics.record_page_visit(page.url)
            except Exception:
                pass

        try:
            budget = max(1, int(max_pages or 8))
        except (TypeError, ValueError):
            budget = 8

        opened = 0
        stuck = 0
        while opened < budget and stuck < 3:
            self._check_skip()
            if page.is_closed():
                break
            await self._wait_full_page_load(page)
            await self.dismiss_popups(page)
            obs = await self._observe_page(page)
            await self._report_page_mode(obs)
            added = await self._act_observed_page(
                page, context, obs,
                visited=visited,
                remaining=budget - opened,
                item_terms=item_terms,
                metrics=metrics,
                target_domain=target_domain,
            )
            if added <= 0:
                stuck += 1
                try:
                    await self.scroll_page(page)
                except Exception:
                    pass
            else:
                stuck = 0
                opened += added

        logger.info(
            f"Observed explore: {len(visited)} unique pages on {target_domain}"
        )

    # ══════════════════════════════════════════════════════════════
    #  TARGETED SITE WARMUP  (Step 2)
    # ══════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    #  SMART SITE STRUCTURE DISCOVERY
    # ══════════════════════════════════════════════════════════════

    # Priority keywords — what a real person would visit (highest first).
    # Covers: SaaS, e-commerce, travel, AI/chat, dev tools, media, gaming, etc.
    _LINK_PRIORITY = {
        # ── CRITICAL (9-10): Pages every evaluating user visits ──
        "pricing": 10, "plans": 10, "price": 10, "prices": 10, "rates": 10,
        "features": 9, "product": 9, "products": 9, "solutions": 9,
        "docs": 9, "documentation": 9, "api": 9, "models": 9,
        # ── TRAVEL / BOOKING / E-COMMERCE (9-10) ──
        "stays": 10, "hotels": 10, "hotel": 10, "flights": 10, "flight": 10,
        "car-rentals": 9, "rentals": 8, "attractions": 9,
        "deals": 10, "offers": 9, "specials": 9, "promotions": 8,
        "destinations": 9, "explore": 8, "search": 8,
        "rooms": 8, "suites": 7, "resorts": 8, "villas": 7, "apartments": 7,
        "airport": 7, "taxi": 7, "taxis": 7, "transfers": 7,
        "shop": 9, "store": 9, "catalog": 9, "catalogue": 9,
        "collections": 8, "categories": 8, "category": 8,
        "bestsellers": 9, "best-sellers": 9, "new-arrivals": 8,
        "sale": 9, "clearance": 8, "outlet": 7,
        # ── GAME SKIN MARKETPLACES (9-10) ──
        "item": 10, "items": 10, "listing": 10, "listings": 9,
        "market": 10, "marketplace": 9, "browse": 9,
        "trade": 8, "trading": 8, "inventory": 9, "loadout": 8,
        "skins": 10, "skin": 9, "unusual": 9, "unusuals": 9,
        "knife": 9, "knives": 9, "gloves": 8, "case": 8, "cases": 8,
        "crate": 7, "crates": 7, "sticker": 7, "stickers": 7,
        "tf2": 9, "rust": 9, "csgo": 9, "cs2": 9,
        "weapons": 8, "hats": 8, "cosmetics": 8, "keys": 7,
        # ── AI / CHAT / TOOLS (9-10) ──
        "chat": 9, "playground": 9, "try": 9, "demo": 9,
        "models": 9, "model": 8, "capabilities": 8,
        "plugins": 8, "extensions": 8, "apps": 8, "tools": 8,
        "prompts": 7, "templates": 7, "examples": 7,
        "research": 8, "papers": 7, "safety": 7,
        "changelog": 7, "releases": 7, "whats-new": 7, "updates": 7,
        # ── HIGH (7-8): Important research pages ──
        "getting-started": 8, "get-started": 8, "quickstart": 8, "quick-start": 8,
        "tutorial": 8, "tutorials": 8, "guide": 8, "guides": 8,
        "how-it-works": 8, "overview": 8, "about": 7,
        "enterprise": 8, "business": 7,
        "integrations": 7, "marketplace": 7,
        "showcase": 7, "use-cases": 7, "gallery": 7,
        "blog": 7, "resources": 7, "learn": 7, "community": 7, "forum": 7,
        "reviews": 7, "testimonials": 7, "customers": 7, "case-studies": 7,
        "compare": 7, "vs": 6, "alternatives": 7, "comparison": 7,
        # ── MEDIUM (4-6): Secondary interest pages ──
        "team": 6, "about-us": 6, "company": 6, "story": 6,
        "faq": 5, "help": 5, "support": 5, "contact": 5,
        "security": 5, "privacy": 5, "trust": 5, "status": 5,
        "news": 5, "press": 5, "media": 5, "events": 5,
        "careers": 4, "jobs": 4, "partners": 4, "affiliates": 4,
        "open-source": 5, "github": 5, "developer": 6, "developers": 6,
        "rewards": 6, "loyalty": 6, "points": 5, "membership": 5,
        "gift-cards": 5, "gift": 5, "coupons": 5, "vouchers": 5,
        "shipping": 5, "delivery": 5, "returns": 5, "warranty": 5,
        "size-guide": 4, "measurements": 4,
        "map": 5, "locations": 5, "nearby": 5, "popular": 6,
        "trending": 6, "top-rated": 7, "recommended": 7,
        # ── LOW (1-2): Rarely visited, but still valid ──
        "legal": 2, "terms": 1, "tos": 1, "privacy-policy": 1,
        "cookies": 1, "sitemap": 1, "accessibility": 2,
        "imprint": 1, "disclaimer": 1,
    }

    # Links to SKIP entirely (auth walls, user-specific pages)
    _SKIP_LINK_KEYWORDS = {
        "login", "log-in", "log_in", "signin", "sign-in", "sign_in",
        "signup", "sign-up", "sign_up", "register", "registration",
        "auth", "oauth", "sso", "callback",
        "account", "my-account", "myaccount", "dashboard", "settings",
        "profile", "password", "forgot", "reset-password", "verify",
        "checkout", "billing", "payment", "subscribe",
        "download", "install", "logout", "log-out", "signout",
        "mailto:", "tel:", "javascript:", "#",
    }

    # Guest cart / bag pages are opened only by maybe_shop_cart, not generic crawls.
    _CART_VIEW_KEYWORDS = {
        "cart", "basket", "trolley", "minicart", "minibag",
        "shopping-bag", "shoppingbag", "shopping_cart",
    }

    _CHECKOUT_SKIP_KEYWORDS = {
        "checkout", "billing", "payment", "place-order", "place_order",
        "placeorder", "paypal", "stripe", "/pay/", "/pays/",
    }

    def _skip_link_keywords(self, allow_cart: bool = False) -> set:
        skip = set(self._SKIP_LINK_KEYWORDS)
        skip.update(self._CHECKOUT_SKIP_KEYWORDS)
        if not allow_cart:
            skip.update(self._CART_VIEW_KEYWORDS)
        if self._link_bias.get("account") or self._link_bias.get("accounts"):
            skip.discard("account")
        return skip

    async def _discover_site_structure(self, page, target_domain: str) -> list:
        """
        Discover the site's structure by extracting and scoring links
        from navigation, header, footer, and hero sections.

        Works for ANY website. Returns a sorted list of
        (score, url, text, source) tuples, highest priority first.

        How it works:
        1. Extract links from <nav>, <header>, [role="navigation"]  (nav links)
        2. Extract links from <footer>  (footer links — usually comprehensive)
        3. Extract CTA/hero links from main content area
        4. Score each link based on its URL path and text
        5. Deduplicate and sort by score

        Returns: [(score, url, link_text, source), ...]
        """
        discovered = {}  # url → (score, text, source)

        # ── 1. Navigation links (highest structural value) ──────
        nav_selectors = [
            "nav a[href]",
            "header a[href]",
            '[role="navigation"] a[href]',
            ".navbar a[href]",
            ".nav a[href]",
            ".navigation a[href]",
            ".header-nav a[href]",
            ".main-nav a[href]",
            ".top-nav a[href]",
        ]
        for sel in nav_selectors:
            try:
                links = await page.query_selector_all(sel)
                for link in links[:30]:
                    await self._score_and_add_link(
                        link, target_domain, discovered, source="nav", bonus=2
                    )
            except Exception:
                continue

        # ── 2. Footer links (often the most complete site map) ──
        footer_selectors = [
            "footer a[href]",
            ".footer a[href]",
            '[role="contentinfo"] a[href]',
            ".site-footer a[href]",
        ]
        for sel in footer_selectors:
            try:
                links = await page.query_selector_all(sel)
                for link in links[:40]:
                    await self._score_and_add_link(
                        link, target_domain, discovered, source="footer", bonus=1
                    )
            except Exception:
                continue

        # ── 3. Hero / CTA links (prominent page links) ──────────
        hero_selectors = [
            "main a[href]",
            ".hero a[href]",
            '[role="main"] a[href]',
            ".cta a[href]",
            "section:first-of-type a[href]",
            "a.btn[href]", "a.button[href]",
            'a[class*="cta"][href]',
            'a[class*="btn-primary"][href]',
        ]
        for sel in hero_selectors:
            try:
                links = await page.query_selector_all(sel)
                for link in links[:20]:
                    await self._score_and_add_link(
                        link, target_domain, discovered, source="hero", bonus=1
                    )
            except Exception:
                continue

        # ── 4. Any remaining visible links on the page ──────────
        try:
            all_links = await page.query_selector_all("a[href]")
            for link in all_links[:60]:
                await self._score_and_add_link(
                    link, target_domain, discovered, source="body", bonus=0
                )
        except Exception:
            pass

        # Convert to sorted list (highest score first)
        result = [
            (score, url, text, source)
            for url, (score, text, source) in discovered.items()
        ]
        result.sort(key=lambda x: x[0], reverse=True)

        return result

    async def _score_and_add_link(self, link_el, target_domain: str,
                                   discovered: dict, source: str, bonus: int):
        """Score a single link element and add it to the discovered dict."""
        try:
            href = await link_el.get_attribute("href") or ""
            if not href or href.startswith("#") or href.startswith("javascript:"):
                return

            # Resolve relative URLs
            if href.startswith("/"):
                href = f"https://{target_domain}{href}"
            elif not href.startswith("http"):
                return

            # Must be on the same domain
            try:
                link_domain = urlparse(href).netloc.replace("www.", "").lower()
            except Exception:
                return
            target_clean = target_domain.replace("www.", "").lower()
            if target_clean not in link_domain and link_domain not in target_clean:
                return

            # Normalize URL (strip query/fragment for dedup)
            normalized = href.split("?")[0].split("#")[0].rstrip("/").lower()

            # Skip auth/blocked links
            path_lower = normalized.lower()
            for kw in self._skip_link_keywords():
                if kw in path_lower:
                    return

            # Get link text
            try:
                text = (await link_el.inner_text() or "").strip()
            except Exception:
                text = ""
            if not text or len(text) < 2 or len(text) > 80:
                return

            # ── Score the link ──────────────────────────────────
            score = bonus  # Start with source bonus

            # Score by URL path segments
            try:
                path = urlparse(normalized).path.lower()
                segments = [s for s in path.split("/") if s]
                for seg in segments:
                    # Check against priority keywords
                    seg_clean = seg.strip("-_")
                    for keyword, priority in self._link_priority_map().items():
                        if keyword in seg_clean:
                            score += priority
                            break
            except Exception:
                pass

            # Score by link text (what users actually read)
            text_lower = text.lower()
            for keyword, priority in self._link_priority_map().items():
                kw_clean = keyword.replace("-", " ").replace("_", " ")
                if kw_clean in text_lower:
                    score += priority * 0.7  # Text match worth 70% of URL match
                    break

            # Minimum score filter (skip completely uninteresting links)
            if score < 1:
                score = 1  # Keep it but at lowest priority

            # Only keep the highest-scored version of each URL
            if normalized not in discovered or discovered[normalized][0] < score:
                discovered[normalized] = (score, text, source)

        except Exception:
            pass

    def _score_link_text(self, text: str) -> int:
        """Quick score from link text alone (used as tiebreaker)."""
        t = text.lower()
        bias = self._link_bias or {}
        if any(w in t for w in ("docs", "documentation", "api", "reference", "quickstart")):
            base = 10 if bias else 9
        elif any(w in t for w in ("pricing", "price", "plans", "cost", "rates")):
            base = 10
        elif any(w in t for w in ("changelog", "blog", "models", "playground")):
            base = 10 if bias else 9
        elif any(w in t for w in ("features", "product", "solutions", "capabilities")):
            base = 9
        elif any(w in t for w in ("stays", "hotels", "flights", "deals", "offers",
                                 "shop", "store", "catalog", "bestseller")):
            base = 9
        elif any(w in t for w in ("chat", "try", "demo")):
            base = 9
        elif any(w in t for w in ("tutorial", "guide", "getting started", "quick start")):
            base = 8
        elif any(w in t for w in ("collections", "categories", "new arrivals", "sale")):
            base = 8
        elif any(w in t for w in ("about", "team", "company", "story", "reviews")):
            base = 6
        elif any(w in t for w in ("news", "updates", "resources")):
            base = 6
        elif any(w in t for w in ("faq", "help", "support", "contact", "rewards")):
            base = 5
        else:
            base = 2
        for kw, extra in bias.items():
            if kw.replace("-", " ") in t or kw in t:
                base = min(10, base + int(extra))
        return base

    async def targeted_site_warmup(self, page, context, target_url: str,
                                    search_queries: list = None,
                                    num_visits: int = 1,
                                    depth_per_visit: tuple = (2, 4),
                                    custom_text: str = "",
                                    metrics=None,
                                    direct_arrival: bool = False,
                                    max_pages: int = None):
        """
        Land on the target site, then explore in-place.

        direct_arrival=True  — open target_url immediately (site-only warmup).
        direct_arrival=False — Google click-through first (full session Step 2).
        """
        self.set_target_host(target_url)
        target_domain = urlparse(target_url).netloc.replace("www.", "")
        if not target_domain:
            target_domain = target_url.replace("https://", "").replace("http://", "").split("/")[0]

        target_domain_core = target_domain.split(".")[0]

        # Generate search queries if not provided
        if not search_queries:
            site_name = target_domain.split(".")[0]
            search_queries = [
                f"{site_name}",
                f"{site_name} pricing",
                f"{site_name} review",
                f"{site_name} features",
                f"{site_name} how to use",
                f"{site_name} getting started",
                f"{site_name} tutorial",
                f"best {site_name} alternatives",
                f"{site_name} docs",
                f"{site_name} documentation",
                f"{site_name} api",
                f"what is {site_name}",
            ]

        # Item-name hints for on-site search (custom focus + short queries).
        item_terms = []
        if custom_text and custom_text.strip():
            item_terms.append(custom_text.strip())
        for q in (search_queries or []):
            words = q.split()
            if 1 <= len(words) <= 4 and target_domain_core not in q.lower():
                item_terms.append(q)
        # De-dupe, keep it small.
        seen_terms = set()
        for kw in list(getattr(self, "_interests", None) or [])[:3]:
            item_terms.append(str(kw))
        item_terms = [t for t in item_terms if not (t.lower() in seen_terms or seen_terms.add(t.lower()))][:8]

        num_visits = 1  # one landing; explore in-place
        landed_ok = False

        for visit_num in range(num_visits):
            self._check_skip()

            if page.is_closed():
                return landed_ok

            arrived = False

            try:
                if direct_arrival:
                    self._report(f"Opening {target_url} directly")
                    logger.info(f"Direct site warmup — navigating to {target_url}")
                    give_up_at = time.monotonic() + 180
                    attempt = 0
                    while time.monotonic() < give_up_at and not arrived:
                        attempt += 1
                        if attempt > 1:
                            self._report(
                                f"Still opening {target_domain} — retry {attempt} "
                                f"(browser stays open)"
                            )
                            await asyncio.sleep(3)
                        ok = await self.safe_navigate(
                            page, target_url, full_load=False, timeout_ms=60000)
                        arrived = (
                            (ok or self._page_matches_url(page, target_url)
                             or await self._page_has_body(page))
                            and not page.is_closed()
                        )
                else:
                    self._report(
                        f"Target visit — Google click-through to {target_domain_core}"
                    )
                    logger.info(
                        f"Target warmup via Google (direct fallback if blocked): "
                        f"{target_domain}"
                    )
                    if not search_queries:
                        search_queries = [self._url_to_search_query(target_url)]
                    query = random.choice(search_queries)
                    arrived = await self.organic_arrival(
                        page, context, target_url, query, first_visit=True)

                    if not arrived and not page.is_closed():
                        self._report(
                            f"Google miss for {target_domain} — opening directly"
                        )
                        logger.info(
                            f"Target warmup Google miss — direct visit: {target_domain}"
                        )
                        arrived = await self._direct_warmup_visit(
                            page, target_url, context=context,
                            depth_override=depth_per_visit,
                        )

                if not arrived or page.is_closed():
                    self._report(
                        f"Target visit failed — {target_domain} not reachable")
                    logger.info(f"Target warmup aborted for {target_domain}")
                    return False
                landed_ok = True

                # Observe this page and act like a visitor of that kind of site.
                await self._wait_full_page_load(page)
                await self.dismiss_popups(page)
                try:
                    explore_n = int(max_pages) if max_pages else 0
                except (TypeError, ValueError):
                    explore_n = 0
                if explore_n <= 0:
                    explore_n = random.randint(
                        max(2, depth_per_visit[0]),
                        max(3, depth_per_visit[1]),
                    )
                await self._explore_observed_site(
                    page, context, target_domain,
                    max_pages=explore_n,
                    item_terms=item_terms,
                    metrics=metrics,
                )

                # ── Break between visits (real user behavior) ──
                if visit_num < num_visits - 1:
                    break_time = random.uniform(10, 40)
                    self._report(f"Taking a break ({int(break_time)}s) — deciding how to re-enter the site...")
                    logger.debug(f"Target warmup: {int(break_time)}s break")
                    await asyncio.sleep(break_time)

            except _SkipPhase:
                raise
            except Exception as e:
                logger.debug(f"Target warmup visit error: {e}")
                continue

        logger.info(f"Target warmup complete on {target_domain}")
        return landed_ok

    # ══════════════════════════════════════════════════════════════
    #  GUEST CART / SALE BROWSING
    #  Optional shopper mimic: sale pages, add/remove in a guest cart.
    #  Login walls or missing cart UI skip ONLY this step — warmup continues.
    #  Never checkout, never pay, never register.
    # ══════════════════════════════════════════════════════════════

    _ADD_TO_CART_SELECTORS = (
        "button:has-text('Add to bag')",
        "button:has-text('Add to cart')",
        "button:has-text('Add to basket')",
        "button:has-text('Add to trolley')",
        "button:has-text('Add to Cart')",
        "button:has-text('Add to Bag')",
        "button:has-text('Add To Cart')",
        "button:has-text('В корзину')",
        "button:has-text('Добавить в корзину')",
        "[data-testid*='add-to-cart' i]",
        "[data-testid*='addToCart' i]",
        "[data-testid*='add-to-bag' i]",
        "[data-testid*='addToBag' i]",
        "[id*='add-to-cart' i]",
        "[id*='addToCart' i]",
        "[class*='add-to-cart' i]",
        "[class*='addToCart' i]",
        "[class*='add-to-bag' i]",
        "button[name*='addToCart' i]",
        "button[name*='add-to-cart' i]",
        "input[type='submit'][value*='Add to' i]",
    )

    _ADD_TO_CART_BLOCK = (
        "buy now", "buy it now", "checkout", "place order", "pay now",
        "wishlist", "wish list", "favorite", "favourite", "notify me",
        "sold out", "out of stock",
    )

    _SALE_LINK_SELECTORS = (
        "a:has-text('Sale')",
        "a:has-text('Deals')",
        "a:has-text('Outlet')",
        "a:has-text('Clearance')",
        "a[href*='/sale' i]",
        "a[href*='outlet' i]",
        "a[href*='clearance' i]",
        "a[href*='deals' i]",
    )

    _CART_OPEN_SELECTORS = (
        "a[href*='/cart' i]",
        "a[href*='/basket' i]",
        "a[href*='/trolley' i]",
        "a:has-text('View bag')",
        "a:has-text('View cart')",
        "a:has-text('View basket')",
        "a:has-text('Shopping bag')",
        "a:has-text('Shopping cart')",
        "button:has-text('View bag')",
        "button:has-text('View cart')",
        "[data-testid*='minicart' i]",
        "[data-testid*='mini-cart' i]",
        "[aria-label='Cart']",
        "[aria-label='Shopping bag']",
        "[aria-label='Shopping cart']",
        "[aria-label='Bag']",
    )

    _CART_REMOVE_SELECTORS = (
        "button:has-text('Remove')",
        "button:has-text('Delete')",
        "a:has-text('Remove')",
        "button:has-text('Удалить')",
        "[aria-label*='Remove' i]",
        "[aria-label*='Delete' i]",
        "[data-testid*='remove' i]",
        "[data-testid*='delete-item' i]",
    )

    def _page_host(self, page) -> str:
        try:
            return urlparse(page.url or "").netloc.lower().replace("www.", "")
        except Exception:
            return ""

    def _is_checkout_url(self, url: str) -> bool:
        u = (url or "").lower()
        return any(k in u for k in self._CHECKOUT_SKIP_KEYWORDS)

    async def _looks_like_auth_wall(self, page, origin_url: str = "") -> bool:
        """True if a login/register page or modal appeared (do not fill it)."""
        if page.is_closed():
            return False
        try:
            current = page.url or ""
            if await self.check_auth_redirect(page, origin_url or current):
                return True
            pw = await page.query_selector("input[type='password']")
            if not pw or not await pw.is_visible():
                return False
            body = ""
            try:
                body = ((await page.inner_text("body")) or "")[:2500].lower()
            except Exception:
                pass
            return any(w in body for w in (
                "sign in", "log in", "sign up", "create account",
                "register", "create an account", "already have an account",
            ))
        except Exception:
            return False

    async def _dismiss_cart_auth(self, page, origin_url: str = ""):
        """Close a login modal or leave a login URL; never fill signup fields."""
        try:
            await self.dismiss_popups(page)
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass
        try:
            if await self._looks_like_auth_wall(page, origin_url):
                await page.go_back()
                await asyncio.sleep(random.uniform(0.8, 2.0))
                await self.dismiss_popups(page)
        except Exception:
            pass

    async def _find_add_to_cart_button(self, page):
        if page.is_closed():
            return None
        for sel in self._ADD_TO_CART_SELECTORS:
            try:
                els = await page.query_selector_all(sel)
            except Exception:
                continue
            for el in els[:8]:
                try:
                    if not await el.is_visible():
                        continue
                    disabled = await el.get_attribute("disabled")
                    aria = (await el.get_attribute("aria-disabled") or "").lower()
                    if disabled is not None or aria in ("true", "1"):
                        continue
                    label = " ".join((
                        (await el.inner_text() or ""),
                        (await el.get_attribute("aria-label") or ""),
                        (await el.get_attribute("value") or ""),
                    )).lower()
                    if any(b in label for b in self._ADD_TO_CART_BLOCK):
                        continue
                    if "add" not in label and "корзин" not in label:
                        # data-testid matches may have empty text
                        if "add" not in sel.lower() and "cart" not in sel.lower() and "bag" not in sel.lower():
                            continue
                    return el
                except Exception:
                    continue
        return None

    async def _shop_has_catalog(self, page) -> bool:
        try:
            cards = await self._find_item_cards(page)
            if cards:
                return True
        except Exception:
            pass
        try:
            host = self._page_host(page)
            hrefs = await self._collect_listing_hrefs(page, host)
            if len(hrefs) >= 2:
                return True
        except Exception:
            pass
        return await self._find_add_to_cart_button(page) is not None

    async def _click_sale_nav(self, page) -> bool:
        for sel in self._SALE_LINK_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el or not await el.is_visible():
                    continue
                href = (await el.get_attribute("href") or "").lower()
                text = (await el.inner_text() or "").lower()
                if any(k in href or k in text for k in ("checkout", "login", "sign")):
                    continue
                self._report("Cart: opening sale / deals")
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 0.9))
                await el.click()
                await self._wait_full_page_load(page, timeout_ms=12000)
                await self.dismiss_popups(page)
                return True
            except Exception:
                continue
        return False

    async def _open_shop_product(self, page) -> bool:
        cards = []
        try:
            cards = await self._find_item_cards(page) or []
        except Exception:
            cards = []
        if cards:
            pick = random.choice(cards[:12])
            try:
                link = await pick.query_selector("a[href]") or pick
                await pick.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 1.0))
                await link.click()
                await self._wait_full_page_load(page, timeout_ms=12000)
                await self.dismiss_popups(page)
                return True
            except Exception:
                pass
        try:
            hrefs = await self._collect_listing_hrefs(page, self._page_host(page))
        except Exception:
            hrefs = []
        if hrefs:
            href = random.choice(hrefs[:15])
            ok = await self.safe_navigate(page, href)
            if ok:
                await self.dismiss_popups(page)
                return True
        return False

    async def _pick_product_variant(self, page):
        """Pick a size/color if the shop requires it before add-to-cart."""
        for sel in (
            "select[name*='size' i]", "select[id*='size' i]",
            "select[name*='colour' i]", "select[name*='color' i]",
        ):
            try:
                box = await page.query_selector(sel)
                if not box or not await box.is_visible():
                    continue
                options = await box.query_selector_all("option")
                enabled = []
                for opt in options[1:8]:
                    dis = await opt.get_attribute("disabled")
                    val = await opt.get_attribute("value")
                    if dis is None and val:
                        enabled.append(val)
                if enabled:
                    await box.select_option(value=random.choice(enabled))
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                    return
            except Exception:
                continue
        try:
            buttons = await page.query_selector_all(
                "[data-testid*='size' i] button, [class*='size'] button, "
                "button[aria-label*='size' i], [role='radio']"
            )
        except Exception:
            buttons = []
        candidates = []
        for btn in buttons[:20]:
            try:
                if not await btn.is_visible():
                    continue
                if await btn.get_attribute("disabled") is not None:
                    continue
                aria = (await btn.get_attribute("aria-disabled") or "").lower()
                if aria in ("true", "1"):
                    continue
                label = ((await btn.inner_text() or "") + " " +
                         (await btn.get_attribute("aria-label") or "")).lower()
                if any(w in label for w in ("guide", "chart", "notify", "sold")):
                    continue
                candidates.append(btn)
            except Exception:
                continue
        if candidates:
            pick = random.choice(candidates[:8])
            try:
                await pick.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.2, 0.6))
                await pick.click()
                await asyncio.sleep(random.uniform(0.4, 1.0))
            except Exception:
                pass

    async def _click_add_to_cart(self, page) -> bool:
        btn = await self._find_add_to_cart_button(page)
        if not btn:
            return False
        try:
            await btn.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.3, 0.9))
            await btn.hover()
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await btn.click()
            await asyncio.sleep(random.uniform(1.0, 2.5))
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=6000)
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def _open_cart_view(self, page) -> bool:
        origin = page.url
        for sel in self._CART_OPEN_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el or not await el.is_visible():
                    continue
                href = (await el.get_attribute("href") or "").lower()
                text = (await el.inner_text() or "").lower()
                if self._is_checkout_url(href) or "checkout" in text or "pay" in text:
                    continue
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await el.click()
                await asyncio.sleep(random.uniform(1.0, 2.2))
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                if self._is_checkout_url(page.url or ""):
                    self._report("Cart: checkout redirect — going back")
                    await page.go_back()
                    await asyncio.sleep(random.uniform(0.8, 1.8))
                    return False
                if await self._looks_like_auth_wall(page, origin):
                    await self._dismiss_cart_auth(page, origin)
                    self._report("Cart: login wall — skip cart")
                    return False
                self._report("Cart: opened bag")
                await self._realistic_dwell(page, min_s=2, max_s=4)
                return True
            except Exception:
                continue
        return False

    async def _remove_cart_item(self, page) -> bool:
        for sel in self._CART_REMOVE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el or not await el.is_visible():
                    continue
                label = ((await el.inner_text() or "") + " " +
                         (await el.get_attribute("aria-label") or "")).lower()
                if any(w in label for w in ("account", "profile", "all items", "wishlist")):
                    continue
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await el.click()
                await asyncio.sleep(random.uniform(0.8, 2.0))
                self._report("Cart: removed 1")
                return True
            except Exception:
                continue
        return False

    async def maybe_shop_cart(self, page):
        """Try guest add-to-cart on this host once. Never aborts the site visit."""
        self._check_skip()
        if page.is_closed() or self._should_skip_auth_ui(page):
            return
        host = self._page_host(page)
        if not host or host in self._cart_tried_hosts:
            return
        self._cart_tried_hosts.add(host)
        try:
            await self._shop_cart_once(page)
        except (_StopRequested, _SkipPhase):
            raise
        except Exception as e:
            logger.debug(f"maybe_shop_cart: {e}")

    async def _shop_cart_once(self, page):
        origin = page.url or ""
        if not await self._shop_has_catalog(page):
            self._report("No cart UI — skip cart")
            return

        if random.random() < 0.55:
            await self._click_sale_nav(page)
            if await self._looks_like_auth_wall(page, origin):
                await self._dismiss_cart_auth(page, origin)
                self._report("Cart: login wall — skip cart")
                return

        added = 0
        want = random.randint(1, 3)
        for n in range(want):
            self._check_skip()
            if page.is_closed():
                return
            if await self._looks_like_auth_wall(page, origin):
                await self._dismiss_cart_auth(page, origin)
                self._report("Cart: login wall — skip cart")
                return

            add_btn = await self._find_add_to_cart_button(page)
            if not add_btn:
                opened = await self._open_shop_product(page)
                if not opened:
                    if n == 0:
                        self._report("No cart UI — skip cart")
                    break
                if await self._looks_like_auth_wall(page, origin):
                    await self._dismiss_cart_auth(page, origin)
                    self._report("Cart: login wall — skip cart")
                    return
                add_btn = await self._find_add_to_cart_button(page)
            if not add_btn:
                if n == 0:
                    self._report("No cart UI — skip cart")
                break

            await self._pick_product_variant(page)
            before = page.url
            if not await self._click_add_to_cart(page):
                break
            if await self._looks_like_auth_wall(page, before):
                await self._dismiss_cart_auth(page, before)
                self._report("Cart: login wall — skip cart")
                return
            if self._is_checkout_url(page.url or ""):
                self._report("Cart: checkout redirect — going back")
                try:
                    await page.go_back()
                    await asyncio.sleep(random.uniform(0.8, 1.8))
                except Exception:
                    pass
                return
            added += 1
            self._report("Cart: added item")
            await asyncio.sleep(random.uniform(0.8, 2.0))

            if n < want - 1:
                try:
                    await page.go_back()
                    await self._wait_full_page_load(page, timeout_ms=10000)
                    await self.dismiss_popups(page)
                except Exception:
                    break

        if added and random.random() < 0.65:
            opened_cart = await self._open_cart_view(page)
            if opened_cart and random.random() < 0.55:
                await self._remove_cart_item(page)

    # ══════════════════════════════════════════════════════════════
    #  GAME-SKIN MARKETPLACE BROWSING
    #  Detects item-listing grids on skin sites (mannco.store, tf2.tm,
    #  skinport, dmarket, etc.) and browses them like a real buyer:
    #  open items, scroll listings, paginate, search, sort/filter.
    # ══════════════════════════════════════════════════════════════

    # CSS selectors that commonly wrap a single tradeable item card.
    _MARKET_CARD_SELECTORS = (
        "[class*='item']", "[class*='Item']",
        "[class*='card']", "[class*='Card']",
        "[class*='product']", "[class*='listing']", "[class*='Listing']",
        "[class*='offer']", "[class*='market']",
        "[class*='inventory']", "[class*='goods']", "[class*='sku']",
        "[data-testid*='item']", "[data-testid*='card']",
        "li[class*='good']", "div[class*='good']",
        "a[href*='/item']", "a[href*='/product']", "a[href*='/listing']",
        "a[href*='/market']", "a[href*='/tf2']", "a[href*='/cs2']",
        "a[href*='/csgo']", "a[href*='/rust']",
    )

    # Price patterns across skin markets ($, €, ₽, keys, ref, metal).
    _PRICE_RE = re.compile(
        r"(\$|€|£|₽|руб|USD|EUR)\s?\d|"
        r"\d+[\.,]?\d*\s?(keys?|ref|refined|metal|₽|\$|€)|"
        r"\b\d+\s?key\b",
        re.IGNORECASE,
    )

    _LISTING_HREF_RE = re.compile(
        r"/(item|items|product|listing|offer|market|inventory|goods|"
        r"tf2|cs2|csgo|rust|unique|unusual)(/|$|\?)",
        re.IGNORECASE,
    )

    def _interest_bias(self, text: str) -> float:
        """Weight for how much this profile's interests match some text.

        Used to make two profiles on the same page click different items —
        each leans toward its own randomly-assigned interests.
        """
        if not text:
            return 1.0
        t = text.lower()
        hits = sum(1 for kw in self._interests if kw in t)
        return 1.0 + hits * 1.5

    async def _find_item_cards(self, page):
        """Heuristically find repeated item cards on a marketplace page.

        A real listing grid = many sibling elements that each contain BOTH an
        image and price-like text. Returns a list of element handles.
        """
        best = []
        for sel in self._MARKET_CARD_SELECTORS:
            try:
                els = await page.query_selector_all(sel)
            except Exception:
                continue
            if len(els) < 6:
                continue

            matches = []
            for el in els[:80]:
                try:
                    if not await el.is_visible():
                        continue
                    has_img = await el.query_selector("img") is not None
                    if not has_img:
                        continue
                    txt = (await el.inner_text() or "").strip()
                    if not txt or len(txt) > 400:
                        continue
                    if self._PRICE_RE.search(txt):
                        matches.append(el)
                except Exception:
                    continue

            # A grid should yield a decent count; keep the richest selector.
            if len(matches) >= 3 and len(matches) > len(best):
                best = matches

        return best

    async def _collect_listing_hrefs(self, page, target_domain: str = "") -> list:
        """Find listing/item URLs on the current page via href patterns.

        Used when CSS card detection misses (custom grids on mannco/tf2.tm).
        """
        try:
            hrefs = await page.evaluate(
                """(domain) => {
                    const origin = location.origin;
                    const pats = /item|product|listing|offer|market|inventory|goods|weapon|hat|unusual|skin|tf2|csgo|cs2|rust|unique/i;
                    const skip = /login|signup|register|checkout|billing|payment|privacy|terms|help|support|javascript:|#/i;
                    const out = [];
                    const seen = new Set();
                    for (const a of document.querySelectorAll('a[href]')) {
                        let href = a.href || '';
                        if (!href || href.indexOf('http') !== 0) continue;
                        if (skip.test(href)) continue;
                        try {
                            const u = new URL(href);
                            if (domain && u.hostname.replace('www.','') !== domain.replace('www.','')) {
                                if (u.origin !== origin) continue;
                            }
                        } catch (e) { continue; }
                        const text = (a.innerText || '').trim();
                        const r = a.getBoundingClientRect();
                        if (r.width < 12 || r.height < 12) continue;
                        if (!(pats.test(href) || pats.test(text))) continue;
                        const key = href.split('#')[0].split('?')[0];
                        if (seen.has(key)) continue;
                        seen.add(key);
                        out.push(href);
                        if (out.length >= 40) break;
                    }
                    return out;
                }""",
                target_domain or "",
            )
            return list(hrefs or [])
        except Exception as e:
            logger.debug(f"listing href collect failed: {e}")
            return []

    async def browse_marketplace_listings(self, page, context, target_domain: str,
                                           item_terms=None, metrics=None,
                                           max_items: int = None):
        """Browse a skin marketplace like a shopper.

        Detects the item grid, opens a random subset of items (dwell + scroll +
        back), scrolls to trigger lazy-loading, advances pagination, and
        occasionally runs an on-site search. Returns True if it looked like a
        marketplace (found item cards), else False so the caller can fall back
        to generic deep exploration.
        """
        item_terms = [t for t in (item_terms or []) if t]
        if max_items is None:
            max_items = random.randint(4, 8)

        # Settle + scroll to let listing grids lazy-load.
        await self._realistic_dwell(page, min_s=2, max_s=4)

        cards = await self._find_item_cards(page)
        listing_hrefs = []
        if not cards:
            for _ in range(2):
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(0.8, 2.0))
            cards = await self._find_item_cards(page)
        if not cards:
            listing_hrefs = await self._collect_listing_hrefs(page, target_domain)
        if not cards and len(listing_hrefs) < 3:
            return False

        n_found = len(cards) if cards else len(listing_hrefs)
        self._report(f"Marketplace listings detected (~{n_found}) — opening items like a buyer")

        # Optional on-site search first (~40%) if we have something to look for.
        if item_terms and random.random() < 0.4:
            await self._marketplace_search(page, random.choice(item_terms))
            await self._realistic_dwell(page, min_s=2, max_s=4)
            cards = await self._find_item_cards(page) or cards

        opened = 0
        pages_seen = 0
        max_pages = 2

        while opened < max_items and pages_seen < max_pages:
            self._check_skip()
            if page.is_closed():
                break

            cards = await self._find_item_cards(page)
            if not cards:
                listing_hrefs = await self._collect_listing_hrefs(page, target_domain)
                if listing_hrefs:
                    random.shuffle(listing_hrefs)
                    for href in listing_hrefs[:min(6, max_items - opened)]:
                        self._check_skip()
                        if page.is_closed():
                            break
                        before_url = page.url
                        ok = await self.safe_navigate(page, href)
                        if not ok:
                            continue
                        opened += 1
                        if metrics:
                            try:
                                metrics.record_link_click()
                                metrics.record_page_visit(page.url)
                            except Exception:
                                pass
                        self._report(f"Opened listing: {href[:60]}")
                        await self._realistic_dwell(page, min_s=2, max_s=4)
                        if page.url != before_url:
                            try:
                                await page.go_back()
                                await self._wait_full_page_load(page)
                                await asyncio.sleep(random.uniform(0.8, 2.2))
                            except Exception:
                                pass
                    pages_seen += 1
                    if opened >= max_items:
                        break
                    if not await self._marketplace_next_page(page):
                        break
                    await self._realistic_dwell(page, min_s=2, max_s=4)
                    continue
                break

            # Pick a handful of items, biased toward this profile's interests
            # (so different profiles click different things on the same page).
            weighted = []
            for card in cards:
                try:
                    txt = (await card.inner_text() or "")[:120]
                except Exception:
                    txt = ""
                weighted.append((self._interest_bias(txt) * random.random(), card))
            weighted.sort(key=lambda x: x[0], reverse=True)
            per_page = min(len(cards), random.randint(3, 6), max_items - opened)
            chosen_cards = [c for _, c in weighted[:per_page]]
            for card in chosen_cards:
                self._check_skip()
                if page.is_closed():
                    break
                try:
                    link = await card.query_selector("a[href]") or card
                    await card.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.3, 1.1))
                    await self.move_mouse_randomly(page)
                    try:
                        await card.hover()
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(0.2, 0.9))

                    before_url = page.url
                    try:
                        await link.click()
                    except Exception:
                        continue

                    await self._wait_full_page_load(page)
                    await self.dismiss_popups(page)

                    if not await self.detect_and_solve_captcha(page):
                        self._report("Captcha on item page — stopping marketplace browse")
                        return True

                    opened += 1
                    if metrics:
                        try:
                            metrics.record_link_click()
                            metrics.record_page_visit(page.url)
                        except Exception:
                            pass

                    # Look over the item: images, price, details.
                    await self._realistic_dwell(page, min_s=2, max_s=4)

                    # Return to the listing (real users bounce back and forth).
                    if page.url != before_url:
                        try:
                            await page.go_back()
                            await self._wait_full_page_load(page)
                            await asyncio.sleep(random.uniform(0.8, 2.2))
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Marketplace item open failed: {e}")
                    continue

            pages_seen += 1

            # Sometimes sort/filter before moving on (~30%).
            if random.random() < 0.3:
                await self._marketplace_apply_filter(page)

            if opened >= max_items:
                break

            # Advance to the next page of listings (button or infinite scroll).
            if not await self._marketplace_next_page(page):
                break
            await self._realistic_dwell(page, min_s=2, max_s=4)

        self._report(f"Marketplace browse done — opened {opened} items across {pages_seen} page(s)")
        try:
            await self.maybe_shop_cart(page)
        except (_StopRequested, _SkipPhase):
            raise
        except Exception:
            pass
        return True

    async def _marketplace_search(self, page, term: str):
        """Type a query into the site's own search box, if one exists."""
        selectors = (
            "input[type='search']",
            "input[name*='search' i]",
            "input[placeholder*='search' i]",
            "input[placeholder*='item' i]",
            "input[aria-label*='search' i]",
            "input[class*='search' i]",
        )
        for sel in selectors:
            try:
                box = await page.query_selector(sel)
                if not box or not await box.is_visible():
                    continue
                self._report(f"Searching on-site for '{term}'")
                await box.scroll_into_view_if_needed()
                await box.click()
                await asyncio.sleep(random.uniform(0.3, 0.9))
                try:
                    await box.fill("")
                except Exception:
                    pass
                await self.type_like_human(page, sel, term)
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await page.keyboard.press("Enter")
                await self._wait_full_page_load(page)
                await self.dismiss_popups(page)
                return True
            except Exception:
                continue
        return False

    async def _marketplace_apply_filter(self, page):
        """Click a sort/filter control to reorder listings (best-effort)."""
        keywords = ("sort", "price", "filter", "newest", "cheap", "popular",
                    "relevance", "order")
        try:
            controls = await page.query_selector_all(
                "button, select, [role='button'], a[class*='sort'], a[class*='filter']"
            )
        except Exception:
            return False
        random.shuffle(controls)
        for ctl in controls[:40]:
            try:
                if not await ctl.is_visible():
                    continue
                label = ((await ctl.inner_text() or "") + " " +
                         (await ctl.get_attribute("aria-label") or "")).lower()
                if not label or not any(k in label for k in keywords):
                    continue
                await ctl.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 0.9))
                await ctl.click()
                self._report(f"Applied listing filter/sort: {label.strip()[:30]}")
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await self.dismiss_popups(page)
                return True
            except Exception:
                continue
        return False

    async def _marketplace_next_page(self, page):
        """Advance listings via a Next/pagination control or infinite scroll."""
        # Try explicit pagination first.
        selectors = (
            "a[rel='next']", "[aria-label*='next' i]", "[class*='next' i]",
            "a[class*='pagination']", "button[class*='pagination']",
        )
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                    await el.click()
                    await self._wait_full_page_load(page)
                    await self.dismiss_popups(page)
                    self._report("Turned to next page of listings")
                    return True
            except Exception:
                continue

        # Fall back to infinite scroll: scroll down and check if height grows.
        try:
            before = await page.evaluate("document.body.scrollHeight")
            for _ in range(random.randint(2, 4)):
                await page.mouse.wheel(0, random.randint(800, 1600))
                await asyncio.sleep(random.uniform(0.8, 2.0))
            after = await page.evaluate("document.body.scrollHeight")
            if after > before + 200:
                self._report("Loaded more listings (infinite scroll)")
                return True
        except Exception:
            pass
        return False

    async def _explore_section_deep(self, page, target_domain: str,
                                      visited_urls: set, depth: int = 2):
        """
        After landing on a section page (e.g., /docs), explore deeper
        within that section by clicking internal sub-links.
        Stays within the same domain and avoids auth pages.
        """
        target_core = target_domain.split(".")[0]

        for i in range(depth):
            self._check_skip()
            if page.is_closed():
                break

            # Find internal links on this page
            try:
                links = await page.query_selector_all("a[href]")
            except Exception:
                break

            candidates = []
            for link in links[:50]:
                try:
                    if not await link.is_visible():
                        continue
                    href = await link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = f"https://{target_domain}{href}"
                    if not href.startswith("http"):
                        continue

                    href_lower = href.lower()
                    normalized = href_lower.split("?")[0].split("#")[0].rstrip("/")

                    # Must be same domain, unvisited, not blocked
                    if (target_domain not in href_lower and target_core not in href_lower):
                        continue
                    if normalized in visited_urls:
                        continue
                    if any(kw in href_lower for kw in self._skip_link_keywords()):
                        continue

                    text = (await link.inner_text() or "").strip()
                    if text and 2 < len(text) < 60:
                        score = self._score_link_text(text)
                        candidates.append((link, href, text, score))
                except Exception:
                    continue

            if not candidates:
                # Scroll to reveal more
                await self.scroll_page(page)
                await asyncio.sleep(random.uniform(1, 2))
                break

            # Sort by score, pick from top candidates (not always #1),
            # nudged by this profile's personal interests.
            candidates.sort(key=lambda x: x[3] * self._interest_bias(x[2]), reverse=True)
            pool = candidates[:min(6, len(candidates))]
            chosen_link, chosen_href, chosen_text, _ = random.choice(pool)

            self._report(f"  → {chosen_text[:35]}")
            try:
                await chosen_link.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.3, 1.0))
                await self.move_mouse_randomly(page)
                await chosen_link.hover()
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await chosen_link.click()

                await self._wait_full_page_load(page)
                await self.dismiss_popups(page)

                norm = chosen_href.split("?")[0].split("#")[0].rstrip("/").lower()
                visited_urls.add(norm)

                # Check still on target domain
                try:
                    if target_domain not in page.url.lower() and target_core not in page.url.lower():
                        await page.go_back()
                        await asyncio.sleep(random.uniform(1, 3))
                        break
                except Exception:
                    break

                if await self.is_dead_page(page):
                    self._report("Dead page — going back")
                    try:
                        await page.go_back()
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                    continue

                # Read this sub-page
                await self._realistic_dwell(page, min_s=2, max_s=5)

                if not await self.detect_and_solve_captcha(page):
                    self._report("Captcha unsolvable — stopping section exploration")
                    break

            except Exception as e:
                logger.debug(f"Section explore click failed: {e}")
                break



class _StopRequested(Exception):
    """Internal signal that warmup should stop."""
    pass


class _SkipPhase(Exception):
    """Internal signal to skip the current phase/site and move on."""
    pass
