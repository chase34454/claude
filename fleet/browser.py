"""Playwright browser tools. Page content is untrusted input to the model.

Approval policy (conservative by design): navigation to public http(s) URLs, reading,
typing without submitting and screenshots are allowed. Any click on something that is
not a plain link (buttons, submit inputs, role=button, anything we cannot find) and any
typing with submit=true needs owner approval.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from urllib.parse import urlparse

from .config import Config
from .tools import Tool, _obj

MAX_TEXT = 20_000

_CLICK_IS_SAFE_JS = """el => {
  const t = el.tagName.toLowerCase();
  return t === 'a' && !!el.getAttribute('href') && el.getAttribute('role') !== 'button';
}"""


def check_url(url: str, allow_private: bool = False) -> None:
    """Raise ValueError unless url is http(s) and (by default) resolves to public IPs.

    Resolution here is not atomic with the browser's own lookup (DNS rebinding); the
    deployment guide recommends network-level egress rules as the real boundary.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"only http(s) URLs are allowed: {url!r}")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {parts.hostname}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"refusing non-public address {ip} for {parts.hostname}")


class BrowserSession:
    """One lazily launched Chromium page per task."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw = self._browser = self._page = None
        self._lock = asyncio.Lock()

    async def page(self):
        async with self._lock:
            if self._page is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                args = ["--no-sandbox"] if self.cfg.browser_no_sandbox else []
                self._browser = await self._pw.chromium.launch(
                    headless=True, executable_path=self.cfg.chromium_path, args=args
                )
                self._page = await self._browser.new_page()
                self._page.set_default_timeout(15_000)
            return self._page

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self._page = None


def browser_tools(session: BrowserSession) -> list[Tool]:
    cfg = session.cfg
    sel = {"selector": {"type": "string", "description": "CSS or text selector"}}

    async def navigate(i: dict) -> str:
        check_url(i["url"], cfg.browser_allow_private)
        page = await session.page()
        resp = await page.goto(i["url"], wait_until="domcontentloaded")
        check_url(page.url, cfg.browser_allow_private)  # re-check after redirects
        return f"{resp.status if resp else '?'} {page.url} title={await page.title()!r}"

    async def read_text(i: dict) -> str:
        page = await session.page()
        text = await page.inner_text(i.get("selector") or "body")
        return text[:MAX_TEXT]

    async def click_needs_approval(i: dict) -> bool:
        page = await session.page()
        loc = page.locator(i["selector"]).first
        return not await loc.evaluate(_CLICK_IS_SAFE_JS)

    async def click(i: dict) -> str:
        page = await session.page()
        await page.locator(i["selector"]).first.click()
        await page.wait_for_load_state("domcontentloaded")
        return f"clicked; now at {page.url}"

    async def type_text(i: dict) -> str:
        page = await session.page()
        await page.locator(i["selector"]).first.fill(i["text"])
        if i.get("submit"):
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return "typed" + (" and submitted" if i.get("submit") else "")

    async def screenshot(_: dict) -> list[dict]:
        page = await session.page()
        png = await page.screenshot(type="png")
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode(),
                },
            }
        ]

    return [
        Tool(
            "browser_navigate",
            "Open an http(s) URL in the browser.",
            _obj({"url": {"type": "string"}}, ["url"]),
            navigate,
            approval=False,
        ),
        Tool(
            "browser_read_text",
            "Read visible text of the page or a selector.",
            _obj({"selector": {"type": "string"}}, []),
            read_text,
            approval=False,
        ),
        Tool(
            "browser_click",
            "Click an element. Buttons and submits need owner approval.",
            _obj(sel, ["selector"]),
            click,
            approval=click_needs_approval,
        ),
        Tool(
            "browser_type",
            "Type into a field; submit=true presses Enter (needs approval).",
            _obj(
                {**sel, "text": {"type": "string"}, "submit": {"type": "boolean"}},
                ["selector", "text"],
            ),
            type_text,
            approval=lambda i: bool(i.get("submit")),
        ),
        Tool(
            "browser_screenshot",
            "Screenshot the current page.",
            _obj({}, []),
            screenshot,
            approval=False,
        ),
    ]
