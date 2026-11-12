"""Daily Telegram digest of the cheapest fuel stations in Vilnius.

Prices come from degalukainos.ena.lt (LEA). On Wednesdays, Viada stations use the
"Super trečiadieniai" prices from viada.lt instead of the configured discount.

Usage:
  python benz.py                 # fetch, compute, send to Telegram
  python benz.py --dry-run       # print the message instead of sending
  python benz.py --brands        # list brands seen in the configured municipalities
  python benz.py --prices-file x.json   # use a saved API response instead of the API
  python benz.py --listen        # keep running and answer /topliak in the Telegram chat
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yaml

TZ = ZoneInfo("Europe/Vilnius")

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
SUPER_WEDNESDAY = "Super trečiadienis"

# Seniūnija boundaries from vilnius21.lt (see build_boundaries.py); stations are placed by coordinates.
# Cheapest price per fuel from the last sent message, to show the change in the next one.
LAST_PRICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_prices.json")
BOUNDARIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seniunijos.json")
OTHER_SENIUNIJA = "Kita"


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
    r = requests.get(VIADA_WEDNESDAY_URL, headers=HEADERS, timeout=30)
    prices = parse_viada_wednesday(r.text, today)
    if not prices:
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        print(
            f"warning: no Viada prices for {today} (HTTP {r.status_code}, "
            f"title {title.group(1).strip() if title else None!r})",
            file=sys.stderr,
        )
    return prices


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


def load_boundaries(path=BOUNDARIES_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def inside(lat, lon, polygon):
    """Ray casting: does the point fall inside a polygon of [lat, lon] points?"""
    result = False
    for (lat1, lon1), (lat2, lon2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (lat1 > lat) != (lat2 > lat) and lon < lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1):
            result = not result
    return result


def seniunija_of(row, boundaries):
    """Name of the seniūnija containing the station's coordinates, or None."""
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return next((name for name, polygon in boundaries.items() if inside(lat, lon, polygon)), None)


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
        if brand and brand.lower() == "viada" and now.weekday() == 2:
            # Super trečiadieniai prices replace the card discount ("nuolaidos nesumuojamos").
            # If the offer page couldn't be read, show the regular price, still without the discount.
            final = viada_prices.get(fuel, price)
            if fuel in viada_prices:
                note = SUPER_WEDNESDAY
        else:
            cents = (discounts.get(brand) or 0) if brand else 0
            final = price - cents / 100
            if cents:
                note = f"−{cents:g} ct"
        results.append({"row": row, "brand": brand, "base": price, "final": round(final, 3), "note": note})

    results.sort(key=lambda r: r["final"])
    return results[: settings.get("top", 10)]


def maps_link(row):
    lat, lon = row.get("latitude"), row.get("longitude")
    if lat and lon:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    q = requests.utils.quote(f"{row.get('company_name', '')}, {row.get('address', '')}")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def group_results(results, boundaries):
    """{final price: {seniūnija: [result]}}, cheapest price first, seniūnijos alphabetical."""
    groups = {}
    for r in results:
        seniunija = seniunija_of(r["row"], boundaries) or OTHER_SENIUNIJA
        groups.setdefault(r["final"], {}).setdefault(seniunija, []).append(r)
    return {price: dict(sorted(by_sen.items())) for price, by_sen in sorted(groups.items())}


def format_station(r, notes):
    row = r["row"]
    brand = r["brand"] or row.get("gas_station_name") or row.get("company_name") or "?"
    addr = html.escape(re.sub(r"^Vilnius,\s*|,\s*\d{5}$", "", (row.get("address") or "").strip()))
    line = f"<i>{html.escape(brand)}</i>"
    if r["note"]:
        # A discount shows the regular price it was taken from; the Viada offer price stands alone.
        base = "" if r["note"] == SUPER_WEDNESDAY else f", {r['base']:.3f}"
        line += f" ({html.escape(r['note'])}{base})"
    note = str((notes or {}).get(r["brand"]) or "").strip()
    if note:
        line += f" · {html.escape(note)}"
    return f'    • {line} — <a href="{maps_link(row)}">{addr or "žemėlapis"}</a>'


def load_last_prices(path=LAST_PRICES_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_last_prices(prices, path=LAST_PRICES_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prices, f)


def price_change(current, previous):
    """" (+3.5 ct)" / " (−1.2 ct)" / " (±0 ct)" against the last message, or "" if unknown."""
    if current is None or previous is None:
        return ""
    cents = round((current - previous) * 100, 1)
    if not cents:
        return " (±0 ct)"
    return f" ({cents:+g} ct)".replace("-", "−")


def format_message(by_fuel, today, is_wednesday, viada_found, boundaries, notes=None, last_prices=None):
    lines = [f"⛽ <b>Pigiausi degalai Vilniuje</b> — {today:%Y-%m-%d}"]
    if is_wednesday:
        lines.append("Viada Super trečiadieniai: " + ("įskaičiuota" if viada_found else "nerasta"))
    for fuel, results in by_fuel.items():
        change = price_change(results[0]["final"] if results else None, (last_prices or {}).get(fuel))
        lines.append(f"\n<b>{FUEL_LABELS.get(fuel, fuel)}</b>{change}")
        if not results:
            lines.append("Nėra duomenų")
        groups = group_results(results, boundaries)
        lowest = next(iter(groups), None)
        for price, by_seniunija in groups.items():
            # Groups above the cheapest show how much more they cost, e.g. "(+0.5 ct) 💶 1.650 €".
            extra = price_change(price, lowest).strip() + " " if price != lowest else ""
            lines.append(f"\n{extra}💶 <b>{price:.3f} €</b>")
            for seniunija, stations in by_seniunija.items():
                lines.append(f"  📍 <b>{html.escape(seniunija)}</b>")
                lines += [format_station(r, notes) for r in stations]
    return "\n".join(lines)


def telegram(method, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=params,
        timeout=params.get("timeout", 0) + 30,
    )
    r.raise_for_status()
    return r.json()["result"]


def send_telegram(text):
    telegram(
        "sendMessage",
        chat_id=os.environ["TELEGRAM_CHAT_ID"],
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def is_command(message, command):
    """True for "/topliak" or "/topliak@SomeBot" sent in the configured chat."""
    if str(message.get("chat", {}).get("id")) != str(os.environ["TELEGRAM_CHAT_ID"]):
        return False
    words = (message.get("text") or "").split()
    return bool(words) and words[0].split("@")[0].lower() == command


def listen(settings, prices_file=None):
    """Long-poll Telegram and send the digest whenever /topliak arrives."""
    telegram("setMyCommands", commands=[{"command": "topliak", "description": "Pigiausi degalai dabar"}])
    print("Listening for /topliak (Ctrl+C to stop)...")
    offset = None
    while True:
        try:
            updates = telegram("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for update in updates:
                offset = update["update_id"] + 1
                if is_command(update.get("message") or {}, "/topliak"):
                    message, prices = build_message(settings, datetime.now(TZ), prices_file, load_last_prices())
                    send_telegram(message)
                    save_last_prices(prices)
        except requests.RequestException as e:
            print(f"warning: Telegram request failed ({e}), retrying", file=sys.stderr)
            time.sleep(5)


def load_rows(prices_file):
    if prices_file:
        with open(prices_file, encoding="utf-8") as f:
            return json.load(f).get("data", [])
    return fetch_ena_prices()


def build_message(settings, now, prices_file=None, last_prices=None):
    """Return the message and {fuel: cheapest final price} to remember for next time."""
    rows = load_rows(prices_file)
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
    message = format_message(
        by_fuel, today, is_wednesday, bool(viada_prices), load_boundaries(), settings.get("notes"), last_prices
    )
    return message, {fuel: results[0]["final"] for fuel, results in by_fuel.items() if results}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--settings", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.yaml"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--brands", action="store_true")
    p.add_argument("--prices-file")
    p.add_argument("--listen", action="store_true")
    args = p.parse_args()

    settings = load_settings(args.settings)
    if args.listen:
        try:
            listen(settings, args.prices_file)
        except KeyboardInterrupt:
            pass
        return

    now = datetime.now(TZ)

    if args.brands:
        municipalities = settings.get("municipalities") or ["Vilniaus m"]
        rows = load_rows(args.prices_file)
        names = sorted({r.get("company_name") or "?" for r in rows if in_municipalities(r, municipalities)})
        print("\n".join(names))
        return

    message, prices = build_message(settings, now, args.prices_file, load_last_prices())

    if args.dry_run:
        print(message)
    else:
        send_telegram(message)
        save_last_prices(prices)


if __name__ == "__main__":
    main()
