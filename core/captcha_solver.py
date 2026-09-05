"""
CAPTCHA solving integration — supports 2Captcha, Anti-Captcha, CapMonster.

2Captcha uses API v2 (createTask / getTaskResult). Google Sorry / Search
requires RecaptchaV2EnterpriseTask with the profile proxy, User-Agent,
google.com cookies, and a fresh data-s on every attempt.
"""

import asyncio
import logging
import re
import threading

import aiohttp

logger = logging.getLogger(__name__)

# ── Service API endpoints ─────────────────────────────────────────
SERVICE_URLS = {
    "2captcha": {
        "create": "https://api.2captcha.com/createTask",
        "result": "https://api.2captcha.com/getTaskResult",
        "report_incorrect": "https://api.2captcha.com/reportIncorrect",
    },
    "anticaptcha": {
        "create": "https://api.anti-captcha.com/createTask",
        "result": "https://api.anti-captcha.com/getTaskResult",
        "report_incorrect": "https://api.anti-captcha.com/reportIncorrect",
    },
    "capmonster": {
        "create": "https://api.capmonster.cloud/createTask",
        "result": "https://api.capmonster.cloud/getTaskResult",
        "report_incorrect": "https://api.capmonster.cloud/reportIncorrect",
    },
}


class CaptchaSolver:
    """Async CAPTCHA solver using external services."""

    def __init__(self, service: str = "2captcha", api_key: str = ""):
        self.service = service.lower().strip()
        self.api_key = api_key.strip()
        self._inflight_lock = threading.Lock()
        self._inflight = 0

    def _inflight_inc(self) -> int:
        with self._inflight_lock:
            self._inflight += 1
            return self._inflight

    def _inflight_dec(self) -> int:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)
            return self._inflight

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.service in SERVICE_URLS

    # ══════════════════════════════════════════════════════════════
    #  GOOGLE "SORRY" PAGE DETECTION
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def url_looks_like_sorry(url: str) -> bool:
        """True only for Google's /sorry/ interstitial, not a normal SERP."""
        u = (url or "").lower()
        if "google.com/sorry" in u:
            return True
        if "ipv4.google.com/sorry" in u or "ipv6.google.com/sorry" in u:
            return True
        if "google." in u and "/sorry/" in u:
            return True
        return False

    @staticmethod
    def _is_google_host(url: str) -> bool:
        u = (url or "").lower()
        return "google." in u or "google.com" in u

    @staticmethod
    async def is_google_sorry_page(page) -> bool:
        """
        Detect Google's bot-detection page.

        URL /sorry/ is authoritative. Content match is limited to unusual-traffic
        phrases on a Google host — never the word "captcha" (that matches SERPs).
        """
        try:
            url = page.url.lower()
            logger.debug(f"Checking if Google sorry page: {url[:80]}")

            if CaptchaSolver.url_looks_like_sorry(url):
                logger.info(f"Google sorry page detected by URL: {url[:60]}")
                return True

            if not CaptchaSolver._is_google_host(url):
                return False

            content = await page.content()
            content_lower = content.lower()
            sorry_indicators = (
                "unusual traffic from your computer",
                "our systems have detected unusual traffic",
            )
            for indicator in sorry_indicators:
                if indicator in content_lower:
                    logger.info(
                        f"Google sorry page detected by content indicator: '{indicator}'"
                    )
                    return True

        except Exception as e:
            logger.debug(f"Google sorry page check error: {e}")
        return False

    @staticmethod
    async def _detect_enterprise_recaptcha(page) -> bool:
        """Detect reCAPTCHA Enterprise (Google Sorry pages always use it)."""
        try:
            url = page.url.lower()

            if "google.com/sorry" in url or "/sorry/" in url:
                logger.info("Enterprise reCAPTCHA detected (Google sorry page)")
                return True

            content = await page.content()
            enterprise_indicators = [
                "recaptcha/enterprise",
                "grecaptcha.enterprise",
                "enterprise.js",
                "recaptcha-enterprise",
            ]
            for indicator in enterprise_indicators:
                if indicator in content.lower():
                    logger.info(f"Enterprise reCAPTCHA detected by indicator: {indicator}")
                    return True

            is_enterprise = await page.evaluate("""() => {
                if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                    return true;
                }
                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const s of scripts) {
                    if (s.src.includes('enterprise')) return true;
                }
                const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                for (const f of iframes) {
                    if (f.src.includes('enterprise')) return true;
                }
                return false;
            }""")

            if is_enterprise:
                logger.info("Enterprise reCAPTCHA detected via JavaScript check")
                return True

        except Exception as e:
            logger.debug(f"Enterprise detection error: {e}")

        return False

    @staticmethod
    async def _extract_data_s(page) -> str:
        """
        Extract the data-s parameter from Google sorry pages.
        REQUIRED for Enterprise reCAPTCHA on Google sorry pages.
        Must be re-extracted for every solve attempt (one-shot).
        """
        try:
            el = await page.query_selector("[data-s]")
            if el:
                data_s = await el.get_attribute("data-s")
                if data_s:
                    logger.info(f"data-s found via [data-s] attribute: {data_s[:40]}...")
                    return data_s

            iframes = await page.query_selector_all("iframe[src*='recaptcha']")
            for iframe in iframes:
                src = await iframe.get_attribute("src") or ""
                match = re.search(r'[?&]s=([A-Za-z0-9_=-]+)', src)
                if match:
                    data_s = match.group(1)
                    logger.info(f"data-s found in iframe src: {data_s[:40]}...")
                    return data_s

            content = await page.content()
            patterns = [
                r'data-s="([^"]+)"',
                r"data-s='([^']+)'",
                r'"s"\s*:\s*"([A-Za-z0-9_=-]+)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    data_s = match.group(1)
                    if len(data_s) > 20:
                        logger.info(f"data-s found via pattern: {data_s[:40]}...")
                        return data_s

            data_s = await page.evaluate("""() => {
                const el = document.querySelector('[data-s]');
                if (el) return el.getAttribute('data-s');

                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    const clients = ___grecaptcha_cfg.clients;
                    for (const key in clients) {
                        const client = clients[key];
                        const findS = (obj, depth = 0) => {
                            if (depth > 5 || !obj) return null;
                            if (typeof obj === 'object') {
                                if (obj.s && typeof obj.s === 'string' && obj.s.length > 20) return obj.s;
                                for (const k in obj) {
                                    const result = findS(obj[k], depth + 1);
                                    if (result) return result;
                                }
                            }
                            return null;
                        };
                        const s = findS(client);
                        if (s) return s;
                    }
                }
                return null;
            }""")

            if data_s:
                logger.info(f"data-s found via JavaScript: {data_s[:40]}...")
                return data_s

            logger.debug("No data-s parameter found on page")

        except Exception as e:
            logger.debug(f"data-s extraction error: {e}")

        return ""

    @staticmethod
    async def extract_recaptcha_sitekey(page) -> str:
        """Extract the reCAPTCHA sitekey from the page."""
        try:
            page_url = page.url
            logger.info(f"Extracting sitekey from: {page_url[:80]}")

            el = await page.query_selector("[data-sitekey]")
            if el:
                sitekey = await el.get_attribute("data-sitekey")
                if sitekey:
                    logger.info(f"Sitekey found via data-sitekey: {sitekey[:30]}...")
                    return sitekey

            iframe_selectors = [
                "iframe[src*='recaptcha']",
                "iframe[src*='google.com/recaptcha']",
                "iframe[title*='reCAPTCHA']",
                "iframe[title*='recaptcha']",
            ]
            for selector in iframe_selectors:
                frames = await page.query_selector_all(selector)
                for frame in frames:
                    src = await frame.get_attribute("src") or ""
                    logger.debug(f"Found iframe: {src[:100]}")
                    for pattern in [r'[?&]k=([A-Za-z0-9_-]+)', r'[?&]sitekey=([A-Za-z0-9_-]+)']:
                        match = re.search(pattern, src)
                        if match:
                            sitekey = match.group(1)
                            logger.info(f"Sitekey found in iframe src: {sitekey[:30]}...")
                            return sitekey

            content = await page.content()
            patterns = [
                r"'sitekey'\s*:\s*'([A-Za-z0-9_-]+)'",
                r'"sitekey"\s*:\s*"([A-Za-z0-9_-]+)"',
                r'data-sitekey="([A-Za-z0-9_-]+)"',
                r"recaptcha/api2/anchor\?.*?k=([A-Za-z0-9_-]+)",
                r"recaptcha/enterprise/anchor\?.*?k=([A-Za-z0-9_-]+)",
                r"render=([A-Za-z0-9_-]{30,})",
                r"grecaptcha\.execute\s*\(\s*['\"]([A-Za-z0-9_-]+)['\"]",
                r"enterprise\.execute\s*\(\s*['\"]([A-Za-z0-9_-]+)['\"]",
            ]
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    sitekey = match.group(1)
                    if sitekey not in ("explicit", "onload", "invisible"):
                        logger.info(f"Sitekey found via pattern '{pattern[:30]}': {sitekey[:30]}...")
                        return sitekey

            try:
                sitekey = await page.evaluate("""() => {
                    if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
                        for (const key in ___grecaptcha_cfg.clients) {
                            const client = ___grecaptcha_cfg.clients[key];
                            const findSitekey = (obj, depth = 0) => {
                                if (depth > 5 || !obj || typeof obj !== 'object') return null;
                                if (obj.sitekey) return obj.sitekey;
                                if (obj.k) return obj.k;
                                for (const k in obj) {
                                    const result = findSitekey(obj[k], depth + 1);
                                    if (result) return result;
                                }
                                return null;
                            };
                            const sk = findSitekey(client);
                            if (sk) return sk;
                        }
                    }
                    return null;
                }""")
                if sitekey:
                    logger.info(f"Sitekey found via JS ___grecaptcha_cfg: {sitekey[:30]}...")
                    return sitekey
            except Exception as e:
                logger.debug(f"JS sitekey extraction failed: {e}")

            logger.warning("Could not find sitekey on page")

        except Exception as e:
            logger.debug(f"Sitekey extraction error: {e}")
        return ""

    # ══════════════════════════════════════════════════════════════
    #  PAGE CONTEXT (UA / cookies / proxy)
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def proxy_public_fields(proxy_config: dict) -> dict:
        """Host/type/port/soft for logs — never includes password."""
        cfg = proxy_config if isinstance(proxy_config, dict) else {}
        nested = cfg.get("user_proxy_config")
        if isinstance(nested, dict) and nested:
            cfg = nested
        return {
            "proxy_soft": cfg.get("proxy_soft") or cfg.get("proxySoft") or "",
            "proxy_type": (
                cfg.get("proxy_type") or cfg.get("proxyType") or cfg.get("type") or ""
            ),
            "proxy_host": (
                cfg.get("proxy_host") or cfg.get("proxyAddress")
                or cfg.get("host") or cfg.get("ip") or cfg.get("proxy_ip") or ""
            ),
            "proxy_port": (
                cfg.get("proxy_port") or cfg.get("proxyPort") or cfg.get("port") or ""
            ),
        }

    @staticmethod
    def normalize_proxy(proxy_config: dict) -> dict:
        """Map AdsPower / 2Captcha-style proxy dicts to createTask proxy fields."""
        if not proxy_config or not isinstance(proxy_config, dict):
            return {}
        nested = proxy_config.get("user_proxy_config")
        if isinstance(nested, dict) and nested:
            proxy_config = nested

        def _first(*keys):
            for k in keys:
                v = proxy_config.get(k)
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                return v
            return ""

        p_type = str(_first("proxy_type", "proxyType", "type") or "").lower()
        p_host = str(_first(
            "proxy_host", "proxyAddress", "host", "ip", "proxy_ip",
        ) or "").strip()
        p_port = _first("proxy_port", "proxyPort", "port")
        p_user = str(_first(
            "proxy_user", "proxyLogin", "user", "username", "login",
        ) or "")
        p_pass = str(_first(
            "proxy_password", "proxyPassword", "password", "pass",
        ) or "")
        if not p_host or not p_port:
            return {}
        if p_type in ("https", "http"):
            p_type = "http"
        elif p_type in ("socks5", "socks5h"):
            p_type = "socks5"
        elif p_type in ("socks4",):
            p_type = "socks4"
        else:
            p_type = p_type or "http"
        try:
            port = int(p_port)
        except (TypeError, ValueError):
            return {}
        out = {
            "proxyType": p_type,
            "proxyAddress": p_host,
            "proxyPort": port,
        }
        if p_user:
            out["proxyLogin"] = p_user
        if p_pass:
            out["proxyPassword"] = p_pass
        return out

    @staticmethod
    async def _collect_user_agent(page) -> str:
        try:
            return (await page.evaluate("() => navigator.userAgent")) or ""
        except Exception as e:
            logger.debug(f"User-Agent collection error: {e}")
            return ""

    @staticmethod
    async def _collect_google_cookies(page) -> str:
        """google.com cookies as name=value; name2=value2 for the solver worker."""
        try:
            cookies = await page.context.cookies()
            parts = []
            for c in cookies:
                domain = (c.get("domain") or "").lower()
                if "google" not in domain:
                    continue
                name = c.get("name") or ""
                value = c.get("value") or ""
                if name:
                    parts.append(f"{name}={value}")
            return "; ".join(parts)
        except Exception as e:
            logger.debug(f"Cookie collection error: {e}")
            return ""

    @staticmethod
    async def _apply_solution_cookies(page, cookies) -> None:
        if not cookies:
            return
        parsed = []
        try:
            if isinstance(cookies, str):
                for part in cookies.split(";"):
                    part = part.strip()
                    if "=" not in part:
                        continue
                    name, value = part.split("=", 1)
                    parsed.append({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".google.com",
                        "path": "/",
                    })
            elif isinstance(cookies, list):
                for c in cookies:
                    if isinstance(c, dict) and c.get("name"):
                        parsed.append({
                            "name": c["name"],
                            "value": c.get("value", ""),
                            "domain": c.get("domain") or ".google.com",
                            "path": c.get("path") or "/",
                        })
            if parsed:
                await page.context.add_cookies(parsed)
        except Exception as e:
            logger.debug(f"Apply solution cookies error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  SOLVE CAPTCHA — MAIN ENTRY POINT
    # ══════════════════════════════════════════════════════════════

    async def solve_recaptcha_on_page(self, page, activity_cb=None,
                                      proxy_config=None, profile_id: str = "") -> bool:
        """
        Detect sitekey + fresh data-s → submit to service → inject → submit.
        Returns True only if the page actually left the CAPTCHA / Sorry state.
        """
        if not self.is_configured:
            logger.info("CAPTCHA solver not configured — skipping")
            return False

        def report(text):
            if activity_cb:
                try:
                    activity_cb(text)
                except Exception:
                    pass

        pid = profile_id or "?"
        page_url = page.url
        report("Extracting CAPTCHA sitekey...")

        sitekey = await self.extract_recaptcha_sitekey(page)
        if not sitekey:
            logger.warning(f"[{pid}] Could not extract reCAPTCHA sitekey")
            report("CAPTCHA sitekey not found — skipping")
            return False

        is_enterprise = await self._detect_enterprise_recaptcha(page)
        data_s = await self._extract_data_s(page)
        data_s_prefix = f"{data_s[:24]}..." if data_s else "none"

        user_agent = await self._collect_user_agent(page)
        cookies = await self._collect_google_cookies(page)
        proxy = self.normalize_proxy(proxy_config)
        has_proxy = bool(proxy and proxy.get("proxyAddress") and proxy.get("proxyPort"))
        if is_enterprise:
            task_type = (
                "RecaptchaV2EnterpriseTask" if has_proxy
                else "RecaptchaV2EnterpriseTaskProxyless"
            )
        else:
            task_type = "RecaptchaV2Task" if has_proxy else "RecaptchaV2TaskProxyless"

        logger.info(
            f"[{pid}] Google Sorry solve start: url={page_url[:80]} "
            f"has_proxy={has_proxy} task={task_type} data-s={data_s_prefix}"
        )
        logger.info(
            f"[{pid}] Found sitekey: {sitekey} on {page_url} "
            f"(enterprise={is_enterprise}, has_data_s={bool(data_s)}, "
            f"has_proxy={has_proxy}, cookies={len(cookies)})"
        )
        report(
            f"Sending CAPTCHA to {self.service}"
            f"{'(Enterprise)' if is_enterprise else ''}"
            f"{' + proxy' if has_proxy else ''}"
            f" ({self._inflight} already in flight)..."
        )

        token, task_id, extra_cookies = await self._solve_recaptcha(
            sitekey, page_url,
            is_enterprise=is_enterprise,
            data_s=data_s,
            user_agent=user_agent,
            cookies=cookies,
            proxy=proxy,
        )

        if not token:
            report("CAPTCHA solve failed — no token received")
            return False

        if extra_cookies:
            await self._apply_solution_cookies(page, extra_cookies)

        report("Got CAPTCHA token — injecting...")
        logger.info(f"[{pid}] CAPTCHA token received, injecting into page")

        injected = await self._inject_token_and_submit(page, token)

        if not injected:
            report("CAPTCHA token injection failed")
            if task_id:
                await self._report_incorrect(task_id)
            return False

        report("Waiting for Sorry page to clear...")
        logger.info(f"[{pid}] CAPTCHA token injected, waiting for navigation")

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            try:
                await page.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass

        await asyncio.sleep(1.5)

        final_url = page.url
        still_sorry_url = self.url_looks_like_sorry(final_url)
        is_still_sorry = still_sorry_url or await self.is_google_sorry_page(page)

        if not still_sorry_url and not is_still_sorry:
            logger.info(
                f"[{pid}] CAPTCHA solved — left Sorry page ({final_url[:80]})"
            )
            report("CAPTCHA solved — left Sorry page")
            return True

        logger.warning(
            f"[{pid}] CAPTCHA injection succeeded but page is still Sorry "
            f"({final_url[:80]})"
        )
        report("CAPTCHA submitted but page unchanged")
        if task_id:
            await self._report_incorrect(task_id)
        return False

    # ══════════════════════════════════════════════════════════════
    #  SERVICE DISPATCH
    # ══════════════════════════════════════════════════════════════

    async def _solve_recaptcha(self, sitekey: str, page_url: str,
                               is_enterprise: bool = False, data_s: str = "",
                               user_agent: str = "", cookies: str = "",
                               proxy: dict = None):
        """Returns (token, task_id, extra_cookies)."""
        if self.service == "2captcha":
            return await self._solve_2captcha(
                sitekey, page_url, is_enterprise=is_enterprise, data_s=data_s,
                user_agent=user_agent, cookies=cookies, proxy=proxy,
            )
        if self.service in ("anticaptcha", "capmonster"):
            return await self._solve_anticaptcha_format(
                sitekey, page_url, is_enterprise=is_enterprise, data_s=data_s,
                user_agent=user_agent, cookies=cookies, proxy=proxy,
            )
        return "", None, None

    def _build_task(self, sitekey: str, page_url: str, is_enterprise: bool,
                    data_s: str, user_agent: str, cookies: str, proxy: dict) -> dict:
        has_proxy = bool(proxy and proxy.get("proxyAddress") and proxy.get("proxyPort"))
        if is_enterprise:
            task_type = (
                "RecaptchaV2EnterpriseTask" if has_proxy
                else "RecaptchaV2EnterpriseTaskProxyless"
            )
        else:
            task_type = "RecaptchaV2Task" if has_proxy else "RecaptchaV2TaskProxyless"

        task = {
            "type": task_type,
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "isInvisible": False,
        }
        if data_s:
            task["recaptchaDataSValue"] = data_s
            if is_enterprise:
                task["enterprisePayload"] = {"s": data_s}
        if user_agent:
            task["userAgent"] = user_agent
        if cookies:
            task["cookies"] = cookies
        if has_proxy:
            task["proxyType"] = proxy["proxyType"]
            task["proxyAddress"] = proxy["proxyAddress"]
            task["proxyPort"] = proxy["proxyPort"]
            if proxy.get("proxyLogin"):
                task["proxyLogin"] = proxy["proxyLogin"]
            if proxy.get("proxyPassword"):
                task["proxyPassword"] = proxy["proxyPassword"]
        return task

    async def _solve_2captcha(self, sitekey: str, page_url: str,
                              is_enterprise: bool = False, data_s: str = "",
                              user_agent: str = "", cookies: str = "",
                              proxy: dict = None):
        """2Captcha API v2: createTask + getTaskResult."""
        urls = SERVICE_URLS["2captcha"]
        task = self._build_task(
            sitekey, page_url, is_enterprise, data_s, user_agent, cookies, proxy,
        )
        payload = {"clientKey": self.api_key, "task": task}
        logger.info(
            f"2Captcha createTask: type={task['type']}, sitekey={sitekey[:20]}..., "
            f"enterprise={is_enterprise}, has_data_s={bool(data_s)}, "
            f"has_proxy={bool(proxy)}, has_ua={bool(user_agent)}"
        )
        n = self._inflight_inc()
        logger.info(f"2Captcha in flight: {n}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    urls["create"], json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json(content_type=None)
                    logger.info(f"2Captcha createTask response: {result}")
                    if result.get("errorId", 1) != 0:
                        logger.error(f"2Captcha createTask error: {result}")
                        return "", None, None
                    task_id = result.get("taskId")
                    if not task_id:
                        logger.error(f"2Captcha no taskId: {result}")
                        return "", None, None

                logger.info(f"2Captcha task submitted: {task_id} (in flight: {n})")

                for _ in range(36):  # 36 * 5s = 180s
                    await asyncio.sleep(5)
                    async with session.post(
                        urls["result"],
                        json={"clientKey": self.api_key, "taskId": task_id},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        result = await resp.json(content_type=None)
                        if result.get("errorId", 0) != 0:
                            logger.error(f"2Captcha getTaskResult error: {result}")
                            return "", task_id, None
                        if result.get("status") == "ready":
                            solution = result.get("solution") or {}
                            token = (
                                solution.get("gRecaptchaResponse")
                                or solution.get("token")
                                or ""
                            )
                            extra = solution.get("cookies")
                            return token, task_id, extra

                logger.warning("2Captcha timed out after 3 minutes")
        except Exception as e:
            logger.error(f"2Captcha error: {e}")
        finally:
            left = self._inflight_dec()
            logger.info(f"2Captcha in flight: {left}")
        return "", None, None

    async def _solve_anticaptcha_format(self, sitekey: str, page_url: str,
                                        is_enterprise: bool = False, data_s: str = "",
                                        user_agent: str = "", cookies: str = "",
                                        proxy: dict = None):
        """Anti-Captcha / CapMonster JSON API (proxy-aware)."""
        urls = SERVICE_URLS[self.service]
        task = self._build_task(
            sitekey, page_url, is_enterprise, data_s, user_agent, cookies, proxy,
        )
        payload = {"clientKey": self.api_key, "task": task}
        logger.info(
            f"{self.service} createTask: type={task['type']}, "
            f"sitekey={sitekey[:20]}..., has_data_s={bool(data_s)}, "
            f"has_proxy={bool(proxy)}"
        )
        n = self._inflight_inc()
        logger.info(f"{self.service} in flight: {n}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    urls["create"], json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json(content_type=None)
                    if result.get("errorId", 1) != 0:
                        logger.error(f"{self.service} submit error: {result}")
                        return "", None, None
                    task_id = result.get("taskId")
                    if not task_id:
                        logger.error(f"{self.service} no taskId: {result}")
                        return "", None, None

                logger.info(f"{self.service} task submitted: {task_id} (in flight: {n})")

                for _ in range(36):
                    await asyncio.sleep(5)
                    async with session.post(
                        urls["result"],
                        json={"clientKey": self.api_key, "taskId": task_id},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        result = await resp.json(content_type=None)
                        if result.get("errorId", 0) != 0:
                            logger.error(f"{self.service} error: {result}")
                            return "", task_id, None
                        if result.get("status") == "ready":
                            solution = result.get("solution") or {}
                            token = (
                                solution.get("gRecaptchaResponse")
                                or solution.get("token")
                                or ""
                            )
                            extra = solution.get("cookies")
                            return token, task_id, extra

                logger.warning(f"{self.service} timed out after 3 minutes")
        except Exception as e:
            logger.error(f"{self.service} error: {e}")
        finally:
            left = self._inflight_dec()
            logger.info(f"{self.service} in flight: {left}")
        return "", None, None

    async def _report_incorrect(self, task_id) -> None:
        urls = SERVICE_URLS.get(self.service) or {}
        endpoint = urls.get("report_incorrect")
        if not endpoint or not task_id:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json(content_type=None)
                    logger.info(f"{self.service} reportIncorrect({task_id}): {result}")
        except Exception as e:
            logger.debug(f"reportIncorrect error: {e}")

    # ══════════════════════════════════════════════════════════════
    #  TOKEN INJECTION
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    async def _inject_token_and_submit(page, token: str) -> bool:
        """Inject the reCAPTCHA token and submit captcha-form / recaptcha form."""
        try:
            logger.info(f"Injecting token (length={len(token)}) into page: {page.url[:60]}")

            injection_result = await page.evaluate("""(token) => {
                let injected = false;
                let callbackCalled = false;

                const responseFields = document.querySelectorAll(
                    '#g-recaptcha-response, [name="g-recaptcha-response"], textarea.g-recaptcha-response, textarea[id*="g-recaptcha-response"]'
                );
                responseFields.forEach(el => {
                    el.style.display = 'block';
                    el.innerHTML = token;
                    el.value = token;
                    injected = true;
                });

                try {
                    const iframes = document.querySelectorAll('iframe');
                    iframes.forEach(iframe => {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            if (iframeDoc) {
                                const fields = iframeDoc.querySelectorAll('[name="g-recaptcha-response"], textarea');
                                fields.forEach(el => {
                                    if (el.name && el.name.includes('recaptcha')) {
                                        el.value = token;
                                        injected = true;
                                    }
                                });
                            }
                        } catch(e) { /* cross-origin iframe, skip */ }
                    });
                } catch(e) {}

                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    try {
                        const clients = ___grecaptcha_cfg.clients;
                        for (const cIdx in clients) {
                            const client = clients[cIdx];
                            const findAndCallCallback = (obj, depth = 0) => {
                                if (depth > 10 || !obj || typeof obj !== 'object') return false;
                                if (typeof obj.callback === 'function') {
                                    try { obj.callback(token); callbackCalled = true; } catch(e) {}
                                }
                                if (obj.G && typeof obj.G === 'function') {
                                    try { obj.G(token); callbackCalled = true; } catch(e) {}
                                }
                                if (obj.o && typeof obj.o === 'function') {
                                    try { obj.o(token); callbackCalled = true; } catch(e) {}
                                }
                                for (const key in obj) {
                                    if (obj.hasOwnProperty(key) && typeof obj[key] === 'object' && obj[key] !== null) {
                                        findAndCallCallback(obj[key], depth + 1);
                                    }
                                }
                            };
                            findAndCallCallback(client);
                        }
                    } catch(e) { console.log('grecaptcha callback error:', e); }
                }

                try {
                    const widgets = document.querySelectorAll('.g-recaptcha, [data-callback]');
                    widgets.forEach((w) => {
                        const cb = w.getAttribute('data-callback');
                        if (cb && typeof window[cb] === 'function') {
                            window[cb](token);
                            callbackCalled = true;
                        }
                    });
                } catch(e) {}

                try {
                    if (typeof window.onCaptchaFinished === 'function') {
                        window.onCaptchaFinished(token);
                        callbackCalled = true;
                    }
                    if (typeof window.captchaCallback === 'function') {
                        window.captchaCallback(token);
                        callbackCalled = true;
                    }
                    if (typeof window.onRecaptchaSuccess === 'function') {
                        window.onRecaptchaSuccess(token);
                        callbackCalled = true;
                    }
                    if (typeof window.onCaptchaSuccess === 'function') {
                        window.onCaptchaSuccess(token);
                        callbackCalled = true;
                    }
                    if (typeof window.submitCallback === 'function') {
                        window.submitCallback(token);
                        callbackCalled = true;
                    }
                } catch(e) {}

                try {
                    responseFields.forEach(el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    });
                } catch(e) {}

                return { injected, callbackCalled };
            }""", token)

            logger.info(f"Token injection result: {injection_result}")
            await asyncio.sleep(0.4)

            submitted = False

            submit_selectors = [
                "#captcha-form input[type='submit']",
                "form#captcha-form input[type='submit']",
                "input[type='submit']",
                "button[type='submit']",
                "#recaptcha-verify-button",
                "button.rc-button-default",
                "input[value='Submit']",
                "input[value='submit']",
                "form input[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Verify')",
            ]
            for sel in submit_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        logger.info(f"Clicking submit button: {sel}")
                        await btn.click()
                        submitted = True
                        break
                except Exception as e:
                    logger.debug(f"Submit selector {sel} failed: {e}")
                    continue

            if not submitted:
                try:
                    form_submitted = await page.evaluate("""() => {
                        const captchaForm = document.querySelector('#captcha-form, form#captcha-form');
                        if (captchaForm) {
                            captchaForm.submit();
                            return 'captcha_form';
                        }
                        const forms = document.querySelectorAll('form');
                        for (const form of forms) {
                            const recaptchaField = form.querySelector('[name="g-recaptcha-response"]');
                            if (recaptchaField && recaptchaField.value) {
                                form.submit();
                                return 'recaptcha_form';
                            }
                        }
                        const anyForm = document.querySelector('form');
                        if (anyForm) {
                            anyForm.submit();
                            return 'any_form';
                        }
                        return null;
                    }""")
                    if form_submitted:
                        logger.info(f"Form submitted via JS: {form_submitted}")
                        submitted = True
                except Exception as e:
                    logger.debug(f"JS form submit failed: {e}")

            if not submitted:
                try:
                    await page.keyboard.press("Enter")
                    logger.info("Pressed Enter key as fallback submit")
                    submitted = True
                except Exception:
                    pass

            injected_ok = False
            if isinstance(injection_result, dict):
                injected_ok = bool(
                    injection_result.get("injected") or injection_result.get("callbackCalled")
                )
            logger.info(f"Submit attempted: {submitted}, injected={injected_ok}")
            return submitted or injected_ok

        except Exception as e:
            logger.error(f"Token injection error: {e}")
            return False
