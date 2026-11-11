"""Daily Telegram digest of the cheapest fuel stations in Vilnius.

Prices come from degalukainos.ena.lt (LEA). On Wednesdays, Viada stations use the
"Super trečiadieniai" prices from viada.lt instead of the configured discount.

Usage:
  python benz.py                 # fetch, compute, send to Telegram
  python benz.py --dry-run       # print the message instead of sending
  python benz.py --brands        # list brands seen in the configured municipalities
  python benz.py --prices-file x.json   # use a saved API response instead of the API
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yaml

TZ = ZoneInfo("Europe/Vilnius")
SEND_HOUR = 12

ENA_SITE = "https://degalukainos.ena.lt/"
ENA_API = "https://api-degalukainos.ena.lt/api/v1/read/prices/latest"
# Public read token embedded in the ENA site bundle; refreshed from the site on each run.
ENA_FALLBACK_TOKEN = "1|KIyMz1NebDWVphuzh7sDy68ANnqoTTABXh1uO4xN067fec57"
VIADA_WEDNESDAY_URL = "https://www.viada.lt/akcija/super-treciadieniai/"

FUEL_LABELS = {"benzinas_95": "95", "dyzelinas": "Dyzelinas", "snd": "LPG"}
# Viada page wording -> ENA fuel_type
VIADA_FUELS = {
    "benzinas_95": r"Visas benzinas[^–-]*[–-]\s*([\d,\.]+)",
    "dyzelinas": r"(?<!Žymėtas )Dyzelinas[^–-]*[–-]\s*([\d,\.]+)",
    "snd": r"Dujos[^–-]*[–-]\s*([\d,\.]+)",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (benz fuel digest)"}


def load_settings(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ena_token():
    """Find the API token in the site's JS bundle, falling back to the known one."""
    try:
        index = requests.get(ENA_SITE, headers=HEADERS, timeout=30).text
        entry = re.search(r'src="\./(assets/index-[\w-]+\.js)"', index).group(1)
        entry_js = requests.get(ENA_SITE + entry, headers=HEADERS, timeout=30).text
        app = re.search(r'"\./(FuelPriceSiteApp-[\w-]+\.js)"', entry_js).group(1)
        app_js = requests.get(ENA_SITE + "assets/" + app, headers=HEADERS, timeout=30).text
        return re.search(r'token:"([^"]+)"', app_js).group(1)
    except Exception as e:
        print(f"warning: could not refresh ENA token ({e}), using fallback", file=sys.stderr)
        return ENA_FALLBACK_TOKEN


def fetch_ena_prices():
    r = requests.get(
        ENA_API,
        headers={**HEADERS, "Accept": "application/json", "Authorization": f"Bearer {ena_token()}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def fetch_viada_wednesday(today):
    """Return {fuel_type: price} if the Super trečiadieniai offer is valid today, else {}."""
    page = requests.get(VIADA_WEDNESDAY_URL, headers=HEADERS, timeout=30).text
    return parse_viada_wednesday(page, today)


def parse_viada_wednesday(page, today):
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    valid = re.search(r"galioja:\s*(\d{4}-\d{2}-\d{2})", text)
    if not valid or valid.group(1) != today.isoformat():
        return {}
    prices = {}
    for fuel, pattern in VIADA_FUELS.items():
        m = re.search(pattern, text)
        if m:
            prices[fuel] = float(m.group(1).replace(",", "."))
    return prices


def brand_of(row, discounts):
    """Match a row to a configured brand by case-insensitive substring."""
    haystack = f"{row.get('company_name') or ''} {row.get('gas_station_name') or ''}".lower()
    for brand in discounts:
        if brand.lower() in haystack:
            return brand
    return None


def in_municipalities(row, municipalities):
    m = (row.get("municipality") or "").lower()
    return any(x.lower() in m for x in municipalities)


def is_fresh(row, now, max_age_hours):
    try:
        submitted = datetime.fromisoformat(row["submitted_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return True
    if submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=TZ)
    return now - submitted <= timedelta(hours=max_age_hours)


def cheapest(rows, settings, fuel, now, viada_prices):
    discounts = settings.get("discounts_cents") or {}
    municipalities = settings.get("municipalities") or ["Vilniaus m"]
    max_age = settings.get("max_age_hours", 36)

    results = []
    for row in rows:
        if row.get("fuel_type") != fuel or not in_municipalities(row, municipalities):
            continue
        if not is_fresh(row, now, max_age):
            continue
        try:
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        brand = brand_of(row, discounts)
        note = ""
        if brand and brand.lower() == "viada" and fuel in viada_prices:
            # Super trečiadieniai prices replace the card discount ("nuolaidos nesumuojamos").
            final = viada_prices[fuel]
            note = "Super trečiadienis"
        else:
            cents = (discounts.get(brand) or 0) if brand else 0
            final = price - cents / 100
            if cents:
                note = f"−{cents:g} ct"
        results.append({"row": row, "base": price, "final": round(final, 3), "note": note})

    results.sort(key=lambda r: r["final"])
    return results[: settings.get("top", 5)]


def maps_link(row):
    lat, lon = row.get("latitude"), row.get("longitude")
    if lat and lon:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    q = requests.utils.quote(f"{row.get('company_name', '')}, {row.get('address', '')}")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def format_message(by_fuel, today, is_wednesday, viada_found):
    lines = [f"⛽ <b>Pigiausi degalai Vilniuje</b> — {today:%Y-%m-%d}"]
    if is_wednesday:
        lines.append("Viada Super trečiadieniai: " + ("įskaičiuota" if viada_found else "nerasta"))
    for fuel, results in by_fuel.items():
        lines.append(f"\n<b>{FUEL_LABELS.get(fuel, fuel)}</b>")
        if not results:
            lines.append("Nėra duomenų")
        for i, r in enumerate(results, 1):
            row = r["row"]
            name = html.escape(row.get("gas_station_name") or row.get("company_name") or "?")
            addr = html.escape(row.get("address") or "")
            price = f"<b>{r['final']:.3f} €</b>"
            if r["note"]:
                price += f" ({html.escape(r['note'])}, be nuol. {r['base']:.3f})"
            lines.append(f'{i}. {price}\n    <a href="{maps_link(row)}">{name}</a>, {addr}')
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def scheduled_for_other_offset(now):
    """GitHub cron is UTC, so the workflow fires at both 09:00 and 10:00 UTC.
    Only the run whose cron hour equals 12:00 Vilnius time today should send."""
    schedule = os.environ.get("SCHEDULE", "").split()
    if len(schedule) < 2 or not schedule[1].isdigit():
        return False  # manual run: always send
    expected_utc_hour = (SEND_HOUR - int(now.utcoffset().total_seconds() // 3600)) % 24
    return int(schedule[1]) != expected_utc_hour


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--settings", default="settings.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--brands", action="store_true")
    p.add_argument("--prices-file")
    args = p.parse_args()

    now = datetime.now(TZ)
    if scheduled_for_other_offset(now):
        print("Not the 12:00 Vilnius run for the current UTC offset, skipping.")
        return

    settings = load_settings(args.settings)
    if args.prices_file:
        with open(args.prices_file, encoding="utf-8") as f:
            rows = json.load(f).get("data", [])
    else:
        rows = fetch_ena_prices()

    if args.brands:
        municipalities = settings.get("municipalities") or ["Vilniaus m"]
        names = sorted({r.get("company_name") or "?" for r in rows if in_municipalities(r, municipalities)})
        print("\n".join(names))
        return

    today = now.date()
    is_wednesday = today.weekday() == 2
    viada_prices = {}
    if is_wednesday:
        try:
            viada_prices = fetch_viada_wednesday(today)
        except requests.RequestException as e:
            print(f"warning: Viada page failed ({e})", file=sys.stderr)

    fuels = settings.get("fuels") or ["benzinas_95"]
    by_fuel = {fuel: cheapest(rows, settings, fuel, now, viada_prices) for fuel in fuels}
    message = format_message(by_fuel, today, is_wednesday, bool(viada_prices))

    if args.dry_run:
        print(message)
    else:
        send_telegram(message)


if __name__ == "__main__":
    main()
