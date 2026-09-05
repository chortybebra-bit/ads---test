# AdsPower Warmup Manager

Desktop app that runs human-like warmup sessions on [AdsPower](https://www.adspower.com/)
browser profiles via Playwright + Dear PyGui.

**Current release: v4.0.1**

## What it does

- Launches selected AdsPower profiles and warms them up in parallel.
- Full sessions use session shapes (ramp / recon / idle / YouTube / search burst)
  with a hard minute timer.
- **Site Warmup** opens one URL directly, then explores that site in place.
- A **page observer** looks at the live DOM and picks a mode:
  shopper (cards / cart UI), docs reader (sidebar / code / article),
  or explorer (prominent in-page links). No hardcoded `/product` path list.
- Optional **2Captcha** (API v2) for Google Sorry / reCAPTCHA.
- Health score prefers cookies on the **target host** (`_fbp`, `_fbc`, `_ga`),
  not Google/YouTube cookie count.

## Personas

- Skin Trader
- Gift Cards / Game Keys
- Account Buyer
- AI Dev
- Social / Meta

No checkout, payment, or seller-panel login.

## Project structure

- `app.py` — Dear PyGui desktop UI.
- `core/warmup_engine.py` — orchestration, phases, late-join profiles, timers.
- `core/human_sim.py` — browsing, page observer, search, human timing.
- `core/browser_manager.py` — AdsPower local API and CDP connect.
- `core/personas.py` — personas, queries, sites.
- `core/session_store.py` — session memory and cookie scoring.
- `core/config_manager.py` — config defaults and persistence.
- `core/captcha_solver.py` — 2Captcha / Anti-Captcha / CapMonster.
- `core/notifications.py` — Windows toast notifications.

## Requirements

See `requirements.txt`:

- `dearpygui`
- `playwright`
- `playwright-stealth`
- `aiohttp`
- `aiohttp-socks`

## Quick start

1. `pip install -r requirements.txt`
2. `playwright install chromium`
3. `python app.py`

AdsPower desktop must be running (local API `http://local.adspower.net:50325`).
Keep `config.json`, `data/`, and `*.log` local — they are git-ignored.

---

## Releases

### v4.0.1

Compared to **v3.0.9**. This is a major engine rewrite, not a Booking patch.

**Site Warmup**

- Direct open of the URL you typed (no Google hunt on the Site Warmup button).
- Failed open is **FAIL**, not a fake OK. A 20s `goto` timeout no longer closes
  the browser and marks success if the page already committed / has a body.
- Retries the same URL for up to 3 minutes while the browser stays open.
- Full-session Step 2 still tries Google first; when the timer is almost up it
  still lands on the target (`_last_chance_target_visit`).
- Ambient phases stop 2–5 minutes early so the target visit actually happens.
- Bandwidth saver is **ignored** on Site Warmup so image pixels can fire.

**Page observer (no `/product` allowlist)**

- After every load, classify from the DOM: similar card grids, cart buttons,
  docs sidebar / TOC / code, or generic prominent links.
- **Shopper** clicks those cards, optional one-shot guest cart, never checkout.
- **Docs reader** follows on-screen sidebar/docs links and may type in search.
- **Explorer** follows large same-host main-content links.
- Mode is recomputed after each navigation (shop → About → shop again).

**Viewport**

- Stopped forcing a random 1280×720-style Playwright viewport inside a
  maximized AdsPower window.
- Clears `Emulation.setDeviceMetricsOverride` and uses the real window size.
  Log: `Viewport: WxH (window)`.

**Google Sorry / 2Captcha**

- 2Captcha API v2 `createTask` / `getTaskResult`.
- Google Sorry uses Enterprise reCAPTCHA **with proxy** when AdsPower exposes
  host/port; otherwise proxyless + a warning.
- Sorry = `/sorry/` URL or “unusual traffic” text. The word `captcha` on a
  normal SERP is not a block.
- Fresh `data-s` between retries; success only if the page leaves `/sorry/`.
- Persist 6h `google_blocked_at` only after a real Sorry and 3 failed tokens.
- Captcha jobs run in parallel (lock-free). SearchGate is only a 2–8s stagger.

**Parallel profiles**

- You can **Start** more idle profiles while a run is already going (late-join).
- Site Warmup stays disabled during a full run so it cannot replace the engine.
- AdsPower `start_browser` still spaced (~3s) to avoid API rate limits.

**Cookies and score**

- Score target-host cookies and pixel names (`_fbp`, `_fbc`, `_ga` / `_gid`).
- Google/YouTube cookies are no longer the main prize when a target is set.
- Log example: `Cookies: 14 on shop.com (incl. _fbp, _ga), 31 total`.

**Personas and Booking**

- Booking.com AI / OpenRouter / wishlist flow removed from the live engine.
- Personas are shop / skins / keys / accounts / AI / Social-Meta.
- Session length is a minute timer, not Slow/Medium/Fast idle presets.
- Optional YouTube phase; remarks written back to the AdsPower profile.

**Dependencies**

- Dropped unused `Pillow`, `openpyxl`, `pyinstaller` from `requirements.txt`.

**What v3.0.9 did (and this release changes)**

- v3.0.9 required Google to find the site and failed if `site_map` was empty.
- v4.0.1 Site Warmup is **direct**. Full sessions still prefer Google, then
  open the target anyway before the timer dies.

History before v4.0.1 was removed from this repository.
