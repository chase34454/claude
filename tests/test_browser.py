import glob
import http.server
import os
import threading

import pytest

from fleet.browser import BrowserSession, browser_tools, check_url
from fleet.claude_code import claude_code_tool
from fleet.gate import ApprovalGate

PAGE = b"""<html><title>T</title><body><h1>Hello fleet</h1><a id=l href="/x">link</a>
<form action="/submit" method="get"><input id=q name=q><button id=go>Go</button></form>
<button id=plain type=button>plain</button></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        H.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    H.hits.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://x/y",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "javascript:alert(1)",
    ],
)
def test_check_url_blocks(url):
    with pytest.raises(ValueError):
        check_url(url)


def test_check_url_allow_private_flag():
    check_url("http://127.0.0.1:8000/", allow_private=True)


def find_chromium():
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    hits = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome")))
    return hits[-1] if hits else None


def chromium_ok():
    return find_chromium() is not None


@pytest.mark.browser
@pytest.mark.skipif(not chromium_ok(), reason="no local chromium")
async def test_browser_tools_and_submit_gating(server, cfg, store):
    from dataclasses import replace

    session = BrowserSession(replace(cfg, chromium_path=find_chromium()))
    tools = {t.name: t for t in browser_tools(session)}
    try:
        out = await tools["browser_navigate"].handler({"url": server + "/"})
        assert "200" in out and "'T'" in out
        assert "Hello fleet" in await tools["browser_read_text"].handler({})
        shot = await tools["browser_screenshot"].handler({})
        assert shot[0]["source"]["media_type"] == "image/png"
        # links are safe, buttons / submits need approval, typing only when submit=true
        assert not await tools["browser_click"].requires_approval({"selector": "#l"})
        assert await tools["browser_click"].requires_approval({"selector": "#go"})
        assert await tools["browser_click"].requires_approval({"selector": "#plain"})
        assert await tools["browser_click"].requires_approval({"selector": "#missing"})
        assert not await tools["browser_type"].requires_approval({"selector": "#q", "text": "a"})
        assert await tools["browser_type"].requires_approval(
            {"selector": "#q", "text": "a", "submit": True}
        )
        await tools["browser_type"].handler({"selector": "#q", "text": "abc"})
        # the gate denies a form submit by default (timeout), so the request is never made
        tid = store.add_task("t", 1.0)
        gate = ApprovalGate(store, tid, timeout_s=0.05, poll_s=0.01)
        ok, why = await gate.authorize(tools["browser_click"], {"selector": "#go"})
        assert not ok and "default deny" in why
        assert not any(h.startswith("/submit") for h in H.hits)
        # once approved, the submit goes through
        await tools["browser_click"].handler({"selector": "#go"})
        assert any(h.startswith("/submit?q=abc") for h in H.hits)
    finally:
        await session.close()


async def test_navigate_rejects_private_when_not_allowed(cfg):
    from dataclasses import replace

    session = BrowserSession(replace(cfg, browser_allow_private=False))
    nav = {t.name: t for t in browser_tools(session)}["browser_navigate"]
    with pytest.raises(ValueError):
        await nav.handler({"url": "http://127.0.0.1:1/"})


async def test_claude_code_tool_with_fake_binary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = tmp_path / "fakeclaude"
    fake.write_text('#!/bin/sh\necho \'{"result": "ran: \'"$2"\'"}\'\n')
    fake.chmod(0o755)
    tool = claude_code_tool(str(fake), ws)
    assert tool.approval is True
    assert await tool.handler({"prompt": "hi"}) == "ran: hi"
    with pytest.raises(PermissionError):
        await tool.handler({"prompt": "hi", "cwd": "../.."})
