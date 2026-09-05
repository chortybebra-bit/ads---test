"""AdsPower browser management via local HTTP API with retry logic."""

import aiohttp
import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Min seconds between start_browser API calls to avoid "Too many request per second"
MIN_START_INTERVAL = 3.0


class BrowserManagerError(Exception):
    """Raised when a browser operation fails after all retries."""
    pass


class BrowserManager:
    """
    Manages AdsPower browser profiles via the local REST API.
    Endpoints: http://local.adspower.net:50325/api/v1/...
    """

    def __init__(self, base_url: str, retries: int = 3, retry_delay: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.retry_delay = retry_delay
        # threading.Lock: start_browser is called from the main engine loop
        # AND from extra loops created when profiles are added mid-run.
        self._start_lock = threading.Lock()
        self._last_start_time = 0.0
        self._user_id_cache: dict = {}

    def _is_rate_limit(self, msg: str) -> bool:
        return msg and ("too many" in str(msg).lower() or "rate" in str(msg).lower())

    async def _request(self, endpoint: str, params: dict = None) -> dict:
        """Make a GET request to AdsPower API with retries."""
        url = f"{self.base_url}{endpoint}"
        last_error = None
        rate_limit_hit = False

        for attempt in range(1, self.retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            last_error = f"HTTP {resp.status}"
                            logger.warning(f"HTTP error {resp.status} (attempt {attempt}/{self.retries})")
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        try:
                            data = await resp.json()
                        except aiohttp.ContentTypeError:
                            last_error = "Response is not JSON"
                            logger.warning(f"Non-JSON response (attempt {attempt}/{self.retries})")
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        if not isinstance(data, dict):
                            last_error = f"Unexpected response type: {type(data).__name__}"
                            logger.warning(f"Non-dict JSON (attempt {attempt}/{self.retries})")
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        if data.get("code") == 0:
                            return data
                        else:
                            last_error = data.get("msg", "Unknown API error")
                            rate_limit_hit = self._is_rate_limit(last_error)
                            if rate_limit_hit:
                                logger.warning(f"API rate limit (attempt {attempt}/{self.retries}), retrying in 15s...")
                            else:
                                logger.warning(f"API error (attempt {attempt}/{self.retries}): {last_error}")
            except asyncio.TimeoutError:
                last_error = "Request timed out"
                logger.warning(f"Timeout (attempt {attempt}/{self.retries}): {url}")
            except aiohttp.ClientError as e:
                last_error = str(e)
                logger.warning(
                    f"Connection error (attempt {attempt}/{self.retries}): {e}"
                )
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Unexpected error (attempt {attempt}/{self.retries}): {e}"
                )

            if attempt < self.retries:
                delay = 15.0 if rate_limit_hit else self.retry_delay
                await asyncio.sleep(delay)

        raise BrowserManagerError(
            f"Failed after {self.retries} attempts: {last_error}"
        )

    async def _post(self, endpoint: str, payload: dict) -> dict:
        """Make a POST request to AdsPower API with retries."""
        url = f"{self.base_url}{endpoint}"
        last_error = None
        rate_limit_hit = False

        for attempt in range(1, self.retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status != 200:
                            last_error = f"HTTP {resp.status}"
                            logger.warning(f"POST HTTP error {resp.status} (attempt {attempt}/{self.retries})")
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        try:
                            data = await resp.json()
                        except aiohttp.ContentTypeError:
                            last_error = "Response is not JSON"
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        if not isinstance(data, dict):
                            last_error = f"Unexpected response type: {type(data).__name__}"
                            if attempt < self.retries:
                                await asyncio.sleep(self.retry_delay)
                            continue
                        if data.get("code") == 0:
                            return data
                        else:
                            last_error = data.get("msg", "Unknown API error")
                            rate_limit_hit = self._is_rate_limit(last_error)
                            if rate_limit_hit:
                                logger.warning(f"POST API rate limit (attempt {attempt}/{self.retries}), retrying in 15s...")
                            else:
                                logger.warning(f"POST API error (attempt {attempt}/{self.retries}): {last_error}")
            except asyncio.TimeoutError:
                last_error = "Request timed out"
                logger.warning(f"POST Timeout (attempt {attempt}/{self.retries}): {url}")
            except aiohttp.ClientError as e:
                last_error = str(e)
                logger.warning(f"POST Connection error (attempt {attempt}/{self.retries}): {e}")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"POST Unexpected error (attempt {attempt}/{self.retries}): {e}")

            if attempt < self.retries:
                delay = 15.0 if rate_limit_hit else self.retry_delay
                await asyncio.sleep(delay)

        raise BrowserManagerError(f"POST failed after {self.retries} attempts: {last_error}")

    async def resolve_user_id(self, serial_or_id: str) -> str:
        """Return AdsPower user_id for a serial number (e.g. '236') or UUID.

        Remark/update APIs require the UUID user_id, not the serial number.
        Results are cached for the lifetime of this BrowserManager.
        """
        sid = str(serial_or_id or "").strip()
        if not sid:
            return ""
        cached = self._user_id_cache.get(sid)
        if cached:
            return cached
        # AdsPower user_ids are alphanumeric tokens, not short integer serials
        if not sid.isdigit():
            self._user_id_cache[sid] = sid
            return sid

        uid = await self._lookup_user_id(sid)
        if uid:
            self._user_id_cache[sid] = uid
        return uid

    async def _lookup_user_id(self, serial: str) -> str:
        try:
            data = await self._request(
                "/api/v1/user/list",
                {"serial_number": serial, "page_size": 1},
            )
            for p in data.get("data", {}).get("list", []) or []:
                if str(p.get("serial_number") or "") == serial:
                    uid = str(p.get("user_id") or "")
                    if uid:
                        return uid
        except BrowserManagerError:
            pass

        page = 1
        while page <= 20:
            try:
                profiles = await self.list_profiles(page=page, page_size=100)
            except BrowserManagerError:
                break
            if not profiles:
                break
            for p in profiles:
                sn = str(p.get("serial_number") or "")
                uid = str(p.get("user_id") or "")
                if sn and uid:
                    self._user_id_cache[sn] = uid
                if sn == serial and uid:
                    return uid
            if len(profiles) < 100:
                break
            page += 1
        return ""

    async def get_profile_remark(self, user_id: str) -> str:
        """Fetch current remark for a profile by user_id."""
        try:
            data = await self._request("/api/v1/user/list", {"user_id": user_id, "page_size": 1})
            profiles = data.get("data", {}).get("list", [])
            if profiles:
                return profiles[0].get("remark", "")
        except BrowserManagerError:
            logger.warning(f"Could not fetch remark for {user_id}")
        return ""

    async def get_profile_proxy(self, user_id: str = "", serial: str = "") -> dict:
        """Re-fetch AdsPower user_proxy_config for a profile. Empty if unmanaged."""
        params = {"page_size": 1}
        if user_id:
            params["user_id"] = user_id
        elif serial:
            params["serial_number"] = serial
        else:
            return {}
        try:
            data = await self._request("/api/v1/user/list", params)
            profiles = data.get("data", {}).get("list", []) or []
            if not profiles:
                return {}
            cfg = profiles[0].get("user_proxy_config") or {}
            return cfg if isinstance(cfg, dict) else {}
        except BrowserManagerError as e:
            logger.warning(
                f"Could not fetch proxy for user_id={user_id or serial}: {e}"
            )
        return {}

    async def update_profile_remark(self, user_id: str, remark: str) -> bool:
        """Update the remark (notes) field of an AdsPower profile."""
        try:
            await self._post("/api/v1/user/update", {
                "user_id": user_id,
                "remark": remark,
            })
            logger.info(f"Remark updated for {user_id}")
            return True
        except BrowserManagerError as e:
            logger.warning(f"Failed to update remark for {user_id}: {e}")
            return False

    @staticmethod
    def _validate_profile_id(profile_id: str) -> str:
        """Validate and sanitize a profile ID to prevent injection."""
        if not profile_id or not isinstance(profile_id, str):
            raise BrowserManagerError("Profile ID must be a non-empty string")
        cleaned = profile_id.strip()
        if not cleaned:
            raise BrowserManagerError("Profile ID must be a non-empty string")
        if len(cleaned) > 100:
            raise BrowserManagerError(f"Profile ID too long: {len(cleaned)} chars")
        return cleaned

    async def _resolve_real_cdp_url(self, debug_port: str) -> str:
        """Query Chrome's /json/version on the debug port to get the real CDP URL.

        When AdsPower has cdp_mask enabled, the puppeteer URL points to a
        proxy that breaks Playwright's Browser.getVersion handshake.
        The /json/version HTTP endpoint bypasses this proxy and returns
        the real webSocketDebuggerUrl.
        """
        url = f"http://127.0.0.1:{debug_port}/json/version"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ws_url = data.get("webSocketDebuggerUrl", "")
                        if ws_url and "/devtools/browser/" in ws_url:
                            logger.info(f"Resolved real CDP URL via debug port {debug_port}")
                            return ws_url
        except Exception as e:
            logger.debug(f"Could not resolve CDP URL from debug port {debug_port}: {e}")
        return ""

    async def start_browser(self, profile_id: str) -> str:
        """
        Start a browser profile and return the WebSocket CDP endpoint.

        Tries cdp_mask=0 to get a clean CDP URL. If AdsPower still returns
        a masked /session endpoint, falls back to querying the debug port's
        /json/version for the real webSocketDebuggerUrl.
        """
        profile_id = self._validate_profile_id(profile_id)
        loop = asyncio.get_running_loop()
        acquired = False
        acquire_fut = loop.run_in_executor(None, self._start_lock.acquire)
        try:
            await acquire_fut
            acquired = True
        except asyncio.CancelledError:
            if acquire_fut.done() and not acquire_fut.cancelled():
                self._start_lock.release()
            raise
        try:
            now = time.monotonic()
            elapsed = now - self._last_start_time
            if elapsed < MIN_START_INTERVAL:
                wait = MIN_START_INTERVAL - elapsed
                await asyncio.sleep(wait)

            params = {"serial_number": profile_id, "cdp_mask": "0"}
            try:
                data = await self._request("/api/v1/browser/start", params)
            except BrowserManagerError:
                params = {"user_id": profile_id, "cdp_mask": "0"}
                data = await self._request("/api/v1/browser/start", params)

            ws_data = data.get("data", {}).get("ws", {})
            debug_port = data.get("data", {}).get("debug_port", "")

            if isinstance(ws_data, dict):
                ws_endpoint = ws_data.get("puppeteer", "")
            else:
                ws_endpoint = str(ws_data)

            # If the endpoint is masked (/session), try to get the real URL
            if ws_endpoint and "/session" in ws_endpoint and debug_port:
                real_url = await self._resolve_real_cdp_url(str(debug_port))
                if real_url:
                    ws_endpoint = real_url

            if not ws_endpoint:
                raise BrowserManagerError(
                    f"No WebSocket endpoint returned for profile {profile_id}"
                )

            self._last_start_time = time.monotonic()
            logger.info(f"Browser started for {profile_id}: {ws_endpoint}")
            return ws_endpoint
        finally:
            if acquired:
                self._start_lock.release()

    async def get_browser_ws(self, profile_id: str) -> str:
        """Get the WebSocket endpoint for an already-running profile."""
        profile_id = self._validate_profile_id(profile_id)
        params = {"serial_number": profile_id}
        try:
            data = await self._request("/api/v1/browser/active", params)
        except BrowserManagerError:
            params = {"user_id": profile_id}
            data = await self._request("/api/v1/browser/active", params)
        ws_data = data.get("data", {}).get("ws", {})
        if isinstance(ws_data, dict):
            ws_endpoint = ws_data.get("puppeteer", "")
        else:
            ws_endpoint = str(ws_data)
        if not ws_endpoint:
            raise BrowserManagerError(
                f"No WebSocket endpoint for running profile {profile_id}"
            )
        return ws_endpoint

    async def stop_browser(self, profile_id: str):
        """Stop a browser profile."""
        profile_id = self._validate_profile_id(profile_id)
        try:
            params = {"serial_number": profile_id}
            await self._request("/api/v1/browser/stop", params)
            logger.info(f"Browser stopped for {profile_id}")
        except BrowserManagerError:
            try:
                params = {"user_id": profile_id}
                await self._request("/api/v1/browser/stop", params)
                logger.info(f"Browser stopped for {profile_id}")
            except BrowserManagerError as e:
                logger.error(f"Failed to stop browser for {profile_id}: {e}")

    async def check_active(self, profile_id: str) -> bool:
        """Check if a profile browser is currently active."""
        try:
            params = {"serial_number": profile_id}
            data = await self._request("/api/v1/browser/active", params)
            status = data.get("data", {}).get("status", "")
            if status.lower() == "active":
                return True
        except BrowserManagerError:
            pass
        # Fallback: try by user_id
        try:
            params = {"user_id": profile_id}
            data = await self._request("/api/v1/browser/active", params)
            status = data.get("data", {}).get("status", "")
            return status.lower() == "active"
        except BrowserManagerError:
            return False

    async def list_profiles(self, page: int = 1, page_size: int = 100) -> list:
        """
        Fetch the list of profiles from AdsPower.

        Returns:
            List of profile dicts with serial_number, name, user_id, etc.
        """
        params = {"page": page, "page_size": page_size}
        data = await self._request("/api/v1/user/list", params)
        profiles = data.get("data", {}).get("list", [])
        return profiles

    async def test_connection(self) -> bool:
        """Test if the AdsPower API is reachable."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/api/v1/user/list"
                async with session.get(
                    url,
                    params={"page": 1, "page_size": 1},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    return isinstance(data, dict) and data.get("code") == 0
        except Exception:
            return False

    @staticmethod
    async def check_proxy_direct(profile_id: str, proxy_config: dict) -> dict:
        """
        Fast proxy check — tests connectivity WITHOUT opening a browser.
        Connects through the proxy to ipify.org and reads the IP.

        Args:
            profile_id: profile serial number
            proxy_config: dict with proxy_type, proxy_host, proxy_port,
                          proxy_user, proxy_password

        Returns:
            {"profile_id": str, "ok": bool, "ip": str|None, "error": str|None}
        """
        result = {"profile_id": profile_id, "ok": False, "ip": None, "error": None}

        p_type = (proxy_config.get("proxy_type") or "").lower()
        p_host = proxy_config.get("proxy_host") or ""
        p_port = proxy_config.get("proxy_port") or ""
        p_user = proxy_config.get("proxy_user") or ""
        p_pass = proxy_config.get("proxy_password") or ""

        if not p_host or not p_port:
            result["error"] = "No proxy configured"
            return result

        # Build proxy URL
        if p_type in ("socks5", "socks5h"):
            scheme = "socks5"
        elif p_type in ("socks4",):
            scheme = "socks4"
        elif p_type in ("http", "https"):
            scheme = "http"
        else:
            scheme = p_type or "socks5"

        if p_user and p_pass:
            proxy_url = f"{scheme}://{p_user}:{p_pass}@{p_host}:{p_port}"
        else:
            proxy_url = f"{scheme}://{p_host}:{p_port}"

        try:
            # Use aiohttp-socks for SOCKS proxy support
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy_url)

            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    "https://api.ipify.org?format=json",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        import json
                        body = await resp.text()
                        data = json.loads(body)
                        ip = data.get("ip", "")
                        if ip:
                            result["ok"] = True
                            result["ip"] = ip
                        else:
                            result["error"] = "No IP in response"
                    else:
                        result["error"] = f"HTTP {resp.status}"
        except ImportError:
            # aiohttp-socks not installed — only HTTP proxies can work without it
            if p_type in ("socks4", "socks5"):
                result["error"] = "SOCKS proxy requires aiohttp-socks. Install with: pip install aiohttp-socks"
            else:
                # Fallback to plain HTTP proxy
                try:
                    proxy_str = f"http://{p_host}:{p_port}"
                    if p_user and p_pass:
                        proxy_str = f"http://{p_user}:{p_pass}@{p_host}:{p_port}"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            "https://api.ipify.org?format=json",
                            proxy=proxy_str,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status == 200:
                                import json
                                body = await resp.text()
                                data = json.loads(body)
                                ip = data.get("ip", "")
                                if ip:
                                    result["ok"] = True
                                    result["ip"] = ip
                                else:
                                    result["error"] = "No IP in response"
                            else:
                                result["error"] = f"HTTP {resp.status}"
                except Exception as e2:
                    result["error"] = f"Proxy connection failed: {e2}"
        except Exception as e:
            result["error"] = f"Proxy connection failed: {e}"

        return result

