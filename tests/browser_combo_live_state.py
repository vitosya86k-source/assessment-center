from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8030/combo_live.html")
parser.add_argument("--screenshots")
args = parser.parse_args()

live_payload = {
    "elapsed": "00:00:12",
    "audio_only": False,
    "alerts": [],
    "channels": [{"title": "Речь", "icon": "💬", "rows": [
        {"nm": "Темп", "val": "130 сл/мин", "min": 60, "max": 220, "value": 130,
         "zones": [[120, "#00d4aa"], [150, "#ffd93d"], [220, "#ff4757"]], "st": "NORMA"}
    ]}],
}


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    out = Path(args.screenshots) if args.screenshots else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    no_source = browser.new_page(viewport={"width": 390, "height": 844})
    no_source.route("**/telegram-web-app.js", lambda r: r.fulfill(content_type="application/javascript", body=""))
    no_source.route("**/combo_data.json", lambda r: r.fulfill(status=404, body=""))
    no_source.goto(args.base_url, wait_until="domcontentloaded")
    no_source.get_by_text("Нет данных от live-источника").wait_for()
    assert no_source.locator("#rec").inner_text() == "НЕТ ИСТОЧНИКА"
    assert no_source.get_by_text("162 сл/мин").count() == 0
    if out:
        no_source.screenshot(path=str(out / "no-source-390x844.png"), full_page=True)

    live = browser.new_page(viewport={"width": 390, "height": 844})
    live.route("**/telegram-web-app.js", lambda r: r.fulfill(content_type="application/javascript", body=""))
    live.route("**/combo_data.json", lambda r: r.fulfill(json=live_payload))
    live.goto(args.base_url, wait_until="domcontentloaded")
    live.locator("#rec").wait_for()
    live.wait_for_function("document.querySelector('#rec')?.textContent === 'LIVE'")
    assert live.locator(".val", has_text="130 сл/мин").count() == 1
    assert live.locator("#rec").inner_text() == "LIVE"

    demo = browser.new_page(viewport={"width": 390, "height": 844})
    demo.route("**/telegram-web-app.js", lambda r: r.fulfill(content_type="application/javascript", body=""))
    demo.route("**/combo_data.json", lambda r: r.fulfill(status=404, body=""))
    demo.goto(args.base_url + "?demo=1", wait_until="domcontentloaded")
    demo.get_by_text("DEMO · НЕ LIVE-ДАННЫЕ").wait_for()
    demo.locator("b", has_text="162 сл/мин").wait_for()
    assert demo.locator("#rec").inner_text() == "DEMO"
    if out:
        demo.screenshot(path=str(out / "gated-demo-390x844.png"), full_page=True)

    browser.close()

print("Assessment Center live/demo state regression: PASS")
