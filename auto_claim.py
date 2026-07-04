#!/usr/bin/env python3
"""auto_claim.py — background auto-claimer for resolved Polymarket positions.

Most 5-minute BTC Up/Down markets auto-settle and pay out winners without any
manual action. This script is a *safety net*: it periodically opens the
Polymarket portfolio page in a headless Chromium browser (via Playwright) and
clicks any visible "Claim" / "Claim all" buttons for positions that still
need a manual claim transaction.

It uses a persistent browser profile directory so you only need to log into
Polymarket once, interactively:

    python auto_claim.py --setup

This opens a headed (visible) Chromium window pointed at polymarket.com —
log in there (wallet connect / email, whatever your account uses), then
close the window (or press Enter in the terminal) once you're logged in.
The session (cookies/local storage) is persisted to `--profile-dir`
(default: `runtime/pw-profile`).

After that, run the claim loop headless:

    python auto_claim.py                 # loop forever, checking every --interval seconds
    python auto_claim.py --once          # single pass then exit
    python auto_claim.py --interval 60   # custom poll interval

The script is intentionally defensive: any missing selector, timeout, or
navigation error is logged and skipped rather than raised, so a Polymarket UI
change or a transient network hiccup never kills the loop.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auto_claim")

PORTFOLIO_URL = "https://polymarket.com/portfolio"
DEFAULT_PROFILE_DIR = "runtime/pw-profile"
DEFAULT_INTERVAL_SEC = 300
CLAIM_BUTTON_TEXT_RE = r"claim(\s+all)?"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-claim resolved Polymarket positions with Playwright."
    )
    parser.add_argument(
        "--profile-dir",
        default=DEFAULT_PROFILE_DIR,
        help=f"Persistent Chromium profile directory (default: {DEFAULT_PROFILE_DIR}).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between claim-loop passes (default: {DEFAULT_INTERVAL_SEC}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single claim pass and exit (instead of looping forever).",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open a headed browser against the portfolio page to log in interactively, "
        "then exit. Run this once before using the headless claim loop.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-selector timeout in seconds (default: 15).",
    )
    return parser.parse_args()


def run_setup(profile_dir: str) -> None:
    """Open a headed, persistent-profile browser so the user can log in manually."""
    from playwright.sync_api import sync_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    log.info("Opening headed Chromium for interactive login (profile: %s)...", profile_dir)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile_dir, headless=False)
        page = context.new_page()
        try:
            page.goto(PORTFOLIO_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            log.exception("Failed to navigate to portfolio page during setup.")
        input(
            "Log into Polymarket in the opened browser window, then press Enter here "
            "once you're logged in (the session will be saved)..."
        )
        context.close()
    log.info("Setup complete. Session saved to %s.", profile_dir)


def claim_once(profile_dir: str, timeout_sec: float) -> int:
    """Open the portfolio page headless and click any Claim buttons found.

    Returns the number of claim clicks attempted. Never raises — all errors are
    logged and swallowed so the caller's loop keeps running.
    """
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout_sec * 1000
    claimed = 0
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile_dir, headless=True)
            try:
                page = context.new_page()
                page.goto(PORTFOLIO_URL, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_timeout(2000)  # let dynamic content settle
                    buttons = page.get_by_role("button", name=CLAIM_BUTTON_TEXT_RE)
                    count = buttons.count()
                except Exception:
                    log.warning("No claim buttons found (selector lookup failed).", exc_info=True)
                    count = 0

                if count == 0:
                    log.info("No claimable positions found.")

                for i in range(count):
                    try:
                        button = buttons.nth(i)
                        if not button.is_visible():
                            continue
                        label = button.inner_text().strip()
                        button.click(timeout=timeout_ms)
                        page.wait_for_timeout(1500)
                        claimed += 1
                        log.info("Clicked claim button: %r", label)
                    except Exception:
                        log.warning("Failed to click a claim button (index %d).", i, exc_info=True)
                        continue
            finally:
                context.close()
    except Exception:
        log.exception("Claim pass failed (browser/navigation error). Will retry next interval.")
    return claimed


def main() -> None:
    args = parse_args()

    if args.setup:
        run_setup(args.profile_dir)
        return

    if args.once:
        n = claim_once(args.profile_dir, args.timeout)
        log.info("Claim pass complete: %d claim(s) attempted.", n)
        return

    log.info("Starting auto-claim loop (interval=%ss). Ctrl-C to stop.", args.interval)
    try:
        while True:
            n = claim_once(args.profile_dir, args.timeout)
            log.info("Claim pass complete: %d claim(s) attempted.", n)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
