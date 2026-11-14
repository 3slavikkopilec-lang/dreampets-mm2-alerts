import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

SITE_URL = "https://dreampets.gg/mm2/"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = Path("seen_lots.json")
CARD_SELECTOR = os.environ.get("LOT_CARD_SELECTOR", "").strip()
LINK_SELECTOR = os.environ.get("LOT_LINK_SELECTOR", "a[href]").strip()
PRICE_RE = re.compile(
    r"(?:₽|руб\.?|R\$|\$)\s*[\d\s,.]+|[\d\s,.]+\s*(?:₽|руб\.?|R\$|\$)",
    re.I,
)

def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text("utf-8")))
    except (OSError, ValueError, TypeError):
        return set()

def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )

async def scrape(page):
    await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(7000)
    for _ in range(5):
        await page.mouse.wheel(0, 1100)
        await page.wait_for_timeout(500)

    return await page.evaluate(
        """({cardSelector, linkSelector}) => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const priceRe = /(?:₽|руб\\.?|R\\$|\\$)\\s*[\\d\\s,.]+|[\\d\\s,.]+\\s*(?:₽|руб\\.?|R\\$|\\$)/i;
          const out = [], used = new Set();
          for (const a of document.querySelectorAll(linkSelector)) {
            const href = a.href;
            if (!href || !href.startsWith(location.origin)) continue;
            if (/javascript:|#|login|register|cart|support|discord/i.test(href)) continue;
            let card = cardSelector ? a.closest(cardSelector) : null;
            if (!card) card = a.closest('[class*=card], [class*=item], [class*=product], article, li');
            if (!card) card = a.parentElement?.parentElement;
            if (!card) continue;
            const text = clean(card.innerText);
            if (text.length < 4 || text.length > 1200 || !priceRe.test(text)) continue;
            const key = href + '|' + text;
            if (used.has(key)) continue;
            used.add(key);
            const titleNode = card.querySelector('h1,h2,h3,h4,[class*=title],[class*=name]');
            out.push({
              url: href,
              title: clean(titleNode?.innerText || a.innerText || text.split('\\n')[0]).slice(0,160),
              text: text.slice(0,700),
              price: (text.match(priceRe) || ['Цена не распознана'])[0],
              image: card.querySelector('img')?.src || ''
            });
          }
          return out;
        }""",
        {"cardSelector": CARD_SELECTOR, "linkSelector": LINK_SELECTOR},
    )

async def send_lot(session, lot):
    embed = {
        "title": ("🆕 " + (lot["title"] or "Новый лот"))[:256],
        "url": lot["url"],
        "description": lot["text"][:1800] or "Новый лот на DreamPets",
        "color": 5793266,
        "fields": [{"name": "Цена", "value": lot["price"][:100], "inline": True}],
        "footer": {"text": "DreamPets MM2 • автоматический мониторинг"},
    }
    if lot["image"].startswith("https://") or lot["image"].startswith("http://"):
        embed["thumbnail"] = {"url": lot["image"]}
    async with session.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=25) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord HTTP {r.status}: {(await r.text())[:300]}")

async def main():
    if not WEBHOOK_URL.startswith("https://discord.com/api/webhooks/"):
        raise SystemExit("Не задан секрет DISCORD_WEBHOOK_URL.")
    seen = load_seen()
    state_existed = STATE_FILE.exists()
    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                lots = await scrape(page)
                fresh = []
                for lot in lots:
                    raw = lot["url"] + "|" + lot["title"] + "|" + lot["price"]
                    lot_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    if lot_id not in seen:
                        fresh.append((lot_id, lot))

                # First run only creates a baseline, so old listings are not spammed.
                if not state_existed:
                    for lot_id, _ in fresh:
                        seen.add(lot_id)
                    save_seen(seen)
                    print(f"Первый запуск: базово сохранено карточек: {len(fresh)}")
                else:
                    sent = 0
                    for lot_id, lot in fresh:
                        await send_lot(session, lot)
                        seen.add(lot_id)
                        sent += 1
                    save_seen(seen)
                    print(f"Найдено карточек: {len(lots)}; отправлено новых: {sent}")
            finally:
                await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
