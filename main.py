#!/usr/bin/env python3
"""
IPC (Israel Producten Centrum) DPA feed builder
================================================
Haalt dagelijks de Google Shopping-feed van de webshop op (gebouwd door Mandelo,
live gegenereerd) en maakt er een Meta-klare products-feed van:

  output/feed_products.xml   RSS 2.0 + g:-namespace (voor Meta scheduled feed)
  output/feed_products.csv   dezelfde data als CSV
  output/coverage.json       dekkingsrapport (sitemap versus feed) + opschoonstatistiek
  output/last_run.txt        tijdstip + aantallen (monitoring)

Bron van waarheid:
  1. FEED_URL   /api/feed/google-shopping.xml   (id = SKU, prijs, sale_price, voorraad)
  2. /sitemap.xml                               (alleen voor de dekkingscontrole)

BELANGRIJK (pixel-matching): g:id == item_id uit de dataLayer == content_ids van de
Meta-pixel (SKU als string, zoals '09487'). Nooit prefixen of naar getal omzetten.

Wat de transformer aanpast ten opzichte van de bronfeed:
  - brand ontbreekt          -> BRAND_FALLBACK
  - leveranciersnaam vooraan -> uit de titel gehaald (brand blijft zoals in de feed)
  - dubbele ids              -> id opgelost via de JSON-LD van de productpagina
  - product_type             -> een hierarchie-string "Hoofdcategorie > sub > sub"
  - lege beschrijving        -> terugvaltekst
  - custom_label_0..2        -> hoofdcategorie, aanbieding, prijsklasse
Optioneel: GOOGLE_SERVICE_ACCOUNT_JSON + SPREADSHEET_ID zetten een Google Sheet-mirror.
"""

import csv
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

BASE = "https://www.israelwinkel.nl"
FEED_URL = f"{BASE}/api/feed/google-shopping.xml"
SITEMAP_URL = f"{BASE}/sitemap.xml"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
USER_AGENT = "Mozilla/5.0 (compatible; IPCFeedBot/1.0)"

BRAND_FALLBACK = "Israël Producten Centrum"
G = "{http://base.google.com/ns/1.0}"

# Kwaliteitsdrempels: bij overschrijding faalt de run en blijft de vorige feed staan.
MIN_ITEMS = 300
MIN_COVERAGE = 0.85        # aandeel sitemap-productpagina's dat in de feed zit
MAX_OOS_SHARE = 0.60       # meer dan 60% out of stock wijst op een kapotte bron

# Hoofdcategorieen uit het hoofdmenu van de webshop (vergelijking zonder hoofdletters).
TOP_LEVEL = [
    "Wijn", "Voedsel & Keuken", "Jodendom & Israel", "Mooi & Gezond",
    "Sieraden & Mode", "Boeken", "Wonen", "Cadeaus", "Hobby & Wonen",
]
_TOP_LOWER = {t.lower(): t for t in TOP_LEVEL}

# Losse categorienamen in de bron die bij een hoofdcategorie horen.
CATEGORY_ALIASES = {
    "sieraden": "Sieraden & Mode", "mode": "Sieraden & Mode",
    "eten & ingrediënten": "Voedsel & Keuken", "eten": "Voedsel & Keuken", "voedsel": "Voedsel & Keuken",
    "israel": "Jodendom & Israel", "jodendom": "Jodendom & Israel",
    "gezond": "Mooi & Gezond", "hobby": "Hobby & Wonen",
}
FALLBACK_CATEGORY = "Overig"

# Prijsklassen voor custom_label_2 (ondergrens, label).
PRICE_TIERS = [(0, "tot 15 euro"), (15, "15 tot 30 euro"), (30, "30 tot 75 euro"), (75, "vanaf 75 euro")]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------- ophalen ----------

def http_get(url, retries=3, timeout=60):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # netwerk of HTTP-fout: opnieuw proberen
            last = e
    raise RuntimeError(f"ophalen mislukt: {url} ({last})")


def parse_source_feed(xml_bytes):
    """Bronfeed -> lijst dicts. De bron herhaalt xmlns:g per element; ElementTree handelt dat af."""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("geen <channel> in de bronfeed")
    items = []
    for it in channel.findall("item"):
        def one(tag, ns=True):
            e = it.find((G if ns else "") + tag)
            return (e.text or "").strip() if e is not None and e.text else ""

        def many(tag):
            return [(e.text or "").strip() for e in it.findall(G + tag) if (e.text or "").strip()]

        items.append({
            "id": one("id"),
            "group": one("item_group_id"),
            "title": one("title", ns=False),
            "description": (it.findtext("description") or "").strip(),
            "link": one("link", ns=False),
            "image": one("image_link"),
            "additional": many("additional_image_link"),
            "availability": one("availability"),
            "price": one("price"),
            "sale_price": one("sale_price"),
            "brand": one("brand"),
            "mpn": one("mpn"),
            "product_types": many("product_type"),
        })
    return items


def fetch_sitemap_product_urls():
    xml = http_get(SITEMAP_URL).decode("utf-8", "replace")
    return [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if "/producten/" in u]


# ---------- opschonen ----------

def clean_text(s):
    s = _CTRL.sub("", s or "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def clean_title(title, brand, sku=""):
    """Haalt de leveranciersnaam vooraan, een ' - <sku>' achteraan en een dubbele variantnaam weg."""
    t = re.sub(r"\s+", " ", title or "").strip()
    original = t
    if brand and t.lower().startswith(brand.lower()):
        rest = t[len(brand):].lstrip(" -:,")
        if rest:
            t = rest
    if sku and t.endswith(f" - {sku}"):
        t = t[: -len(f" - {sku}")].rstrip()
    parts = [p.strip() for p in t.split(" - ")]
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        t = parts[0]
    t = re.sub(r"\s+-\s+", " - ", t)
    return (t or original)[:200]


def price_number(price_str):
    try:
        return float((price_str or "").split()[0])
    except (ValueError, IndexError):
        return 0.0


def normalize_category(name):
    n = (name or "").strip()
    if n.lower() in _TOP_LOWER:
        return _TOP_LOWER[n.lower()]
    if n.lower() in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[n.lower()]
    return n[:1].upper() + n[1:]


def build_hierarchy(product_types):
    """['ontbijt','jam','Voedsel & Keuken'] -> ('Voedsel & Keuken', 'Voedsel & Keuken > Ontbijt > Jam')."""
    cats = [normalize_category(c) for c in product_types if c]
    if not cats:
        return FALLBACK_CATEGORY, FALLBACK_CATEGORY
    main = next((c for c in cats if c in TOP_LEVEL), cats[0])
    rest = [c for c in dict.fromkeys(cats) if c != main]
    return main, " > ".join([main] + rest)


def price_tier(amount):
    label = PRICE_TIERS[0][1]
    for floor, name in PRICE_TIERS:
        if amount >= floor:
            label = name
    return label


def resolve_pixel_id(link):
    """Voor dubbele ids: neem de SKU van de standaardvariant uit de JSON-LD van de productpagina.
    Dat is dezelfde waarde als item_id in de dataLayer (en dus content_ids van de pixel)."""
    try:
        html = http_get(link.split("?")[0]).decode("utf-8", "replace")
        for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
            data = json.loads(block)
            for x in (data if isinstance(data, list) else [data]):
                if x.get("@type") == "Product":
                    offers = x.get("offers") or []
                    offers = offers if isinstance(offers, list) else [offers]
                    if offers and offers[0].get("sku"):
                        return str(offers[0]["sku"])
    except Exception:
        pass
    return ""


def transform(items, stats):
    """Bronitems -> Meta-items. Slaat items over die Meta niet kan gebruiken en logt de reden."""
    out, skipped = [], []

    # 1. dubbele ids oplossen (vaak twee productpagina's met dezelfde SKU in de bron)
    by_id = defaultdict(list)
    for it in items:
        by_id[it["id"]].append(it)
    for sku, group in by_id.items():
        if len(group) < 2:
            continue
        stats["duplicate_ids"].append(sku)
        for it in group:
            # alleen items waarvan de link NIET ?sku=<id> draagt: dat zijn de losse varianten
            if f"sku={sku}" in it["link"]:
                continue
            resolved = resolve_pixel_id(it["link"])
            if resolved and resolved != sku and resolved not in by_id:
                it["id"] = resolved
                stats["ids_resolved"].append(f"{sku} -> {resolved}")

    seen = set()
    for it in items:
        reason = None
        amount = price_number(it["price"])
        if not it["id"]:
            reason = "geen id"
        elif it["id"] in seen:
            reason = "dubbel id"
        elif not it["link"] or not it["image"]:
            reason = "geen link of afbeelding"
        elif amount <= 0:
            reason = "prijs 0"
        elif not it["title"]:
            reason = "geen titel"
        if reason:
            skipped.append((it["id"] or "?", it["title"][:60], reason))
            continue
        seen.add(it["id"])

        brand = it["brand"] or BRAND_FALLBACK
        if not it["brand"]:
            stats["brand_fallback"] += 1
        title = clean_title(it["title"], it["brand"], it["id"])
        if title != it["title"]:
            stats["titles_cleaned"] += 1
        description = clean_text(it["description"])
        if not description:
            description = f"{title}. Bestel online bij Israelwinkel.nl, het grootste aanbod Israëlische producten."
            stats["description_fallback"] += 1

        sale = it["sale_price"] if it["sale_price"] and price_number(it["sale_price"]) < amount else ""
        main, hierarchy = build_hierarchy(it["product_types"])
        current = price_number(sale) if sale else amount
        out.append({
            "id": it["id"],
            "item_group_id": it["group"],
            "title": title,
            "description": description[:5000],
            "availability": it["availability"] if it["availability"] in ("in stock", "out of stock") else "in stock",
            "condition": "new",
            "price": it["price"],
            "sale_price": sale,
            "link": it["link"],
            "image_link": it["image"],
            "additional_image_link": it["additional"][:10],
            "brand": brand,
            "mpn": it["mpn"],
            "product_type": hierarchy,
            "custom_label_0": main,
            "custom_label_1": "aanbieding" if sale else "regulier",
            "custom_label_2": price_tier(current),
            "custom_number_0": f"{current:.2f}",
        })
    return out, skipped


# ---------- dekking ----------

def coverage_report(feed_items, sitemap_urls):
    feed_pages = {i["link"].split("?")[0] for i in feed_items}
    site_pages = {u.rstrip("/") for u in sitemap_urls}
    missing = sorted(site_pages - feed_pages)
    extra = sorted(feed_pages - site_pages)
    covered = len(site_pages & feed_pages)
    return {
        "sitemap_product_pages": len(site_pages),
        "feed_product_pages": len(feed_pages),
        "covered": covered,
        "coverage": round(covered / len(site_pages), 4) if site_pages else 0,
        "in_sitemap_not_in_feed": [m.replace(BASE, "") for m in missing],
        "in_feed_not_in_sitemap": [e.replace(BASE, "") for e in extra],
    }


# ---------- schrijvers ----------

CSV_HEADERS = [
    "id", "title", "description", "availability", "condition", "price", "sale_price",
    "link", "image_link", "additional_image_link", "brand", "item_group_id", "mpn",
    "product_type", "custom_label_0", "custom_label_1", "custom_label_2", "custom_number_0",
]


def _x(tag, value):
    return f"    <g:{tag}>{escape(_CTRL.sub('', str(value)))}</g:{tag}>"


def rss_item(d):
    p = ["  <item>"]
    p.append(_x("id", d["id"]))
    p.append(_x("title", d["title"]))
    p.append(_x("description", d["description"]))
    p.append(_x("availability", d["availability"]))
    p.append(_x("condition", d["condition"]))
    p.append(_x("price", d["price"]))
    if d["sale_price"]:
        p.append(_x("sale_price", d["sale_price"]))
    p.append(_x("link", d["link"]))
    p.append(_x("image_link", d["image_link"]))
    for img in d["additional_image_link"]:
        p.append(_x("additional_image_link", img))
    p.append(_x("brand", d["brand"]))
    if d["item_group_id"]:
        p.append(_x("item_group_id", d["item_group_id"]))
    if d["mpn"]:
        p.append(_x("mpn", d["mpn"]))
    if d["product_type"]:
        p.append(_x("product_type", d["product_type"]))
    for n in (0, 1, 2):
        if d[f"custom_label_{n}"]:
            p.append(_x(f"custom_label_{n}", d[f"custom_label_{n}"]))
    p.append(_x("custom_number_0", d["custom_number_0"]))
    p.append("  </item>")
    return "\n".join(p)


def write_products_xml(path, rows):
    body = "\n".join(rss_item(d) for d in rows)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
        "  <channel>\n"
        "    <title>Israel Producten Centrum</title>\n"
        f"    <link>{escape(BASE)}</link>\n"
        "    <description>Israelwinkel.nl producten (dagelijks bijgewerkt)</description>\n"
        f"{body}\n"
        "  </channel>\n"
        "</rss>\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)


def csv_row(d):
    return [
        d["id"], d["title"], d["description"], d["availability"], d["condition"], d["price"],
        d["sale_price"], d["link"], d["image_link"], ",".join(d["additional_image_link"]),
        d["brand"], d["item_group_id"], d["mpn"], d["product_type"],
        d["custom_label_0"], d["custom_label_1"], d["custom_label_2"], d["custom_number_0"],
    ]


def write_products_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        for d in rows:
            w.writerow(csv_row(d))


def push_to_sheets(rows):
    """Optioneel: Google Sheet-mirror. Waarden als RAW-tekst zodat '09487' zijn voorloopnul houdt."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not creds_json or not spreadsheet_id:
        print("[sheets] overgeslagen (GOOGLE_SERVICE_ACCOUNT_JSON / SPREADSHEET_ID niet gezet)", flush=True)
        return
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet("IPC")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="IPC", rows=max(10, len(rows) + 5), cols=len(CSV_HEADERS))
    ws.update([CSV_HEADERS] + [csv_row(d) for d in rows], "A1", value_input_option="RAW")
    print(f"[sheets] tab IPC: {len(rows)} items geschreven", flush=True)


# ---------- main ----------

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    source = parse_source_feed(http_get(FEED_URL))
    print(f"[bron] {len(source)} items uit {FEED_URL}", flush=True)

    stats = {
        "duplicate_ids": [], "ids_resolved": [], "brand_fallback": 0,
        "titles_cleaned": 0, "description_fallback": 0,
    }
    rows, skipped = transform(source, stats)
    cov = coverage_report(rows, fetch_sitemap_product_urls())

    in_stock = sum(1 for r in rows if r["availability"] == "in stock")
    oos = len(rows) - in_stock
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_items": len(source), "feed_items": len(rows), "skipped": len(skipped),
        "in_stock": in_stock, "out_of_stock": oos,
        "on_sale": sum(1 for r in rows if r["sale_price"]),
        **stats, "coverage": cov,
        "skipped_detail": [{"id": a, "title": b, "reason": c} for a, b, c in skipped],
    }

    # kwaliteitspoort: bij een kapotte bron blijft de vorige feed in de repo staan
    problems = []
    if len(rows) < MIN_ITEMS:
        problems.append(f"slechts {len(rows)} items (minimum {MIN_ITEMS})")
    if cov["coverage"] < MIN_COVERAGE:
        problems.append(f"dekking {cov['coverage']:.0%} (minimum {MIN_COVERAGE:.0%})")
    if rows and oos / len(rows) > MAX_OOS_SHARE:
        problems.append(f"{oos}/{len(rows)} out of stock (maximum {MAX_OOS_SHARE:.0%})")
    if problems:
        with open(os.path.join(OUT_DIR, "coverage.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        sys.exit("FOUT: kwaliteitscontrole mislukt, feed niet overschreven: " + "; ".join(problems))

    write_products_xml(os.path.join(OUT_DIR, "feed_products.xml"), rows)
    write_products_csv(os.path.join(OUT_DIR, "feed_products.csv"), rows)
    with open(os.path.join(OUT_DIR, "coverage.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "last_run.txt"), "w", encoding="utf-8") as f:
        f.write(f"{summary['generated_utc']}\nItems: {len(rows)}\nIn stock: {in_stock}\n"
                f"Out of stock: {oos}\nSkipped: {len(skipped)}\nCoverage: {cov['coverage']:.1%}\n")

    try:
        push_to_sheets(rows)
    except Exception as e:  # de mirror is optioneel en mag de run niet laten falen
        print(f"[sheets] fout: {e}", flush=True)

    print(f"\nKLAAR: {len(rows)} items ({in_stock} in stock, {oos} out of stock), "
          f"{summary['on_sale']} in de aanbieding, {len(skipped)} overgeslagen", flush=True)
    print(f"Dekking: {cov['covered']}/{cov['sitemap_product_pages']} productpagina's uit de sitemap "
          f"({cov['coverage']:.1%})", flush=True)
    print(f"Opgeschoond: {stats['brand_fallback']}x brand-fallback, {stats['titles_cleaned']}x titel, "
          f"{stats['description_fallback']}x beschrijving, dubbele ids: {stats['duplicate_ids']} "
          f"-> {stats['ids_resolved']}", flush=True)
    if cov["in_sitemap_not_in_feed"]:
        print("In sitemap maar niet in feed:", ", ".join(cov["in_sitemap_not_in_feed"]), flush=True)
    for a, b, c in skipped[:20]:
        print(f"  - overgeslagen {a} {b}: {c}", flush=True)


if __name__ == "__main__":
    main()
