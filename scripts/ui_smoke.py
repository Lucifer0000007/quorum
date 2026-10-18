"""End-to-end UI check in a real browser.

Starts a throwaway server whose episodic graph is isolated (the demo's real episodic memory is never touched),
clicks through the demo flow, asserts what the page shows, then removes the throwaway graph.

    pip install -r requirements-dev.txt && playwright install chromium     # once
    python -m scripts.ui_smoke [--screenshots DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from ingest.config import ROOT, connect

SMOKE_GRAPH = "agent_episodic_memory_uismoke"
VERDICTS = [("4381", "human gate required"), ("624", "at risk"), ("1020", "clean")]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED: {message}")
    print(f"ok   {message}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def drop_graph() -> None:
    assert SMOKE_GRAPH.endswith("uismoke")
    db = connect()
    if SMOKE_GRAPH in db.list_graphs():
        db.select_graph(SMOKE_GRAPH).delete()


def wait_for_health(base: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early:\n{log.read_text(errors='replace')[-1500:]}")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not become healthy in 45 s (is FalkorDB up and the graph ingested?)")


def run_flow(base: str, shots: Path | None) -> None:
    from playwright.sync_api import sync_playwright

    unexpected_console_errors: list[str] = []
    expecting_error = False

    def snap(page, name: str) -> None:
        if shots:
            page.screenshot(path=str(shots / name), full_page=True)

    def preflight(page, kep: str) -> None:
        page.fill("#kep", kep)
        page.click("#kep-form button[type=submit]")
        page.wait_for_function(f"document.querySelector('.verdict .what')?.innerText.includes('KEP-{kep}:')")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        def on_console(msg):
            if msg.type == "error" and not expecting_error:
                unexpected_console_errors.append(msg.text)

        page.on("console", on_console)
        page.on("pageerror", lambda e: unexpected_console_errors.append(str(e)))

        page.goto(base)
        page.wait_for_selector("#st-graph .ok", timeout=15000)
        status = page.inner_text("#status")
        check(re.search(r"\d+ KEPs", status) is not None, "header shows the live graph counts")
        check("Keywords only" in status, "header shows the active mode (no LLM is configured for this run)")
        snap(page, "01-home.png")

        page.click("#examples button:nth-child(3)")
        page.wait_for_selector("#output .answer")
        check(page.inner_text("#output .answer").startswith("Top people for"), "who-knows example answers")
        check(page.locator("#cypher .query").count() >= 1, "the Cypher panel shows what ran")

        page.select_option("#router", "keyword")
        page.fill("#ask", "Does KEP-2377 have a successor?")
        page.click("#ask-form button[type=submit]")
        page.wait_for_function("document.querySelector('#output .answer')?.innerText.includes('2377')")
        check("KEP-2386" in page.inner_text("#output .answer"), "successor of KEP-2377 is KEP-2386")

        page.click("#examples button:has-text('central')")
        page.wait_for_selector("#output th:has-text('PageRank')")
        check(page.locator("#output tbody tr").count() >= 5, "PageRank table lists KEPs")
        check("algo.pageRank" in page.inner_text("#cypher"), "the PageRank Cypher is shown")
        snap(page, "02-central.png")

        for kep, word in VERDICTS:
            preflight(page, kep)
            check(page.inner_text(".verdict .word") == word, f"KEP-{kep} verdict is '{word}'")
            if word == "clean":
                check("No path found" in page.inner_text("#output"), "clean verdict is reported as 'No path found'")
            snap(page, f"03-preflight-{kep}.png")

        preflight(page, "4381")
        before = page.locator("#cypher .query").count()
        page.click("#approve")
        page.wait_for_selector("#approved .recorded")
        approved = page.inner_text("#approved")
        check("Posted externally: no" in approved, "approval says nothing was posted externally")
        check(page.locator("#cypher .query").count() == before + 1, "the write query is appended to the read queries")

        preflight(page, "4381")
        page.wait_for_selector(".precedent")
        check("checked before" in page.inner_text(".precedent"), "repeat check shows the prior-precedent banner")
        snap(page, "04-precedent.png")

        expecting_error = True
        page.fill("#kep", "999999")
        page.click("#kep-form button[type=submit]")
        page.wait_for_selector("#error:not([hidden])")
        check("No KEP with id or number" in page.inner_text("#error"), "unknown KEP fails loudly in the UI")
        expecting_error = False

        phone = browser.new_page(viewport={"width": 390, "height": 844})
        phone.goto(base)
        phone.wait_for_selector("#st-graph .ok")
        preflight(phone, "1287")
        check(phone.evaluate("document.documentElement.scrollWidth") <= 392, "no horizontal scroll at 390 px")
        snap(phone, "05-mobile.png")
        browser.close()

    check(not unexpected_console_errors, f"no unexpected browser console errors {unexpected_console_errors or ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", type=Path, help="write PNG screenshots to this directory")
    args = ap.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    drop_graph()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = Path(tempfile.gettempdir()) / "quorum-ui-smoke-server.log"
    # The throwaway server never gets an LLM (empty values win over .env), so this check is deterministic and free.
    env = os.environ | {"QUORUM_EPISODIC_GRAPH": SMOKE_GRAPH, "PYTHONIOENCODING": "utf-8",
                        "LLM_BASE_URL": "", "LLM_API_KEY": "", "LLM_MODEL": ""}
    with log.open("wb") as out:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent.api:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        try:
            wait_for_health(base, server, log)
            run_flow(base, args.screenshots)
            print("UI smoke test passed.")
            return 0
        except Exception as exc:
            print(exc)
            return 1
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            drop_graph()


if __name__ == "__main__":
    sys.exit(main())
