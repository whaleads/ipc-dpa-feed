# IPC (Israel Producten Centrum) Dynamic Product Ads feed

![Feed status](https://github.com/whaleads/ipc-dpa-feed/actions/workflows/daily-feed.yml/badge.svg)

Dagelijkse transformer die de Google Shopping-feed van israelwinkel.nl omzet naar een Meta-klare products-feed (RSS 2.0 met `g:`-namespace, plus CSV).

| Bestand | Inhoud |
|---|---|
| `output/feed_products.xml` | Meta scheduled feed (primair) |
| `output/feed_products.csv` | Dezelfde data als CSV |
| `output/coverage.json` | Dekkingsrapport (sitemap versus feed) en opschoonstatistiek |
| `output/last_run.txt` | Tijdstip en aantallen van de laatste run |

Feed-URL voor Meta:
```
https://raw.githubusercontent.com/whaleads/ipc-dpa-feed/main/output/feed_products.xml
```

100% gratis stack: GitHub Actions (dagelijkse cron) + Python (alleen standaardbibliotheek) + feeds als bestand in de repo. Google Sheet-mirror is optioneel.

---

## 1. Waarom een transformer en geen crawler

Onderzoek op 29-10-2026 (alleen lezen, niets gewijzigd):

- **De webshop heeft al een feed**, gebouwd door Mandelo: `https://www.israelwinkel.nl/api/feed/google-shopping.xml`. Ze wordt bij elk verzoek live gegenereerd, dus prijs en voorraad zijn zo actueel als de site. 600 items, 417 productgroepen, `id` = SKU.
- **Google Merchant Center (340947279) haalt precies deze URL op**, dagelijks om 00:00 CET ("PRODUCTS SOURCE 3", 600 producten verwerkt, geen bestandsproblemen).
- **Bigshopper (Google CSS) leest uit Merchant Center.** Er is geen eigen productbron en geen Meta-export, alleen een aanvullende label-feed voor Google. Bigshopper is dus maximaal zo actueel als de laatste Merchant Center-fetch (tot 24 uur achter).
- **Het CMS (Mandelo) heeft geen feed- of catalogusmodule.** Prijs en voorraad komen uit een aparte commerce-backend.
- De JSON-LD op de productpagina's is **niet** bruikbaar voor voorraad: alle 611 varianten staan op `InStock`, ook uitverkochte. De feed heeft de voorraad wel goed (7 van 7 steekproeven kloppen met de site).

Daarom hoeft er niet gecrawld te worden (geen Playwright). De transformer haalt de bronfeed op, maakt hem geschikt voor Meta en bewaakt de kwaliteit.

## 2. Wat de transformer aanpast

| Onderdeel | Bronfeed | Meta-feed |
|---|---|---|
| `brand` | ontbreekt bij 231 van 600 | `Israël Producten Centrum` als terugval |
| `title` | begint met de juridische leveranciersnaam ("Sano International ltd Careline ...") | leveranciersnaam, ` - <sku>` en dubbele variantnaam eruit |
| Dubbele ids | 75226, 75227, 75228 komen twee keer voor | losse oude producten krijgen de SKU uit hun JSON-LD (`75226..`), zoals de pixel die stuurt |
| `product_type` | losse elementen in wisselende volgorde | een string: `Voedsel & Keuken > Ontbijt > Jam` |
| `description` | 1 item leeg | terugvaltekst |
| Labels | geen | `custom_label_0` hoofdcategorie, `_1` aanbieding/regulier, `_2` prijsklasse, `custom_number_0` actuele prijs |

Ongewijzigd: `id`, `item_group_id`, `link` (met `?sku=`), `image_link`, `additional_image_link` (maximaal 10), `price`, `sale_price`, `availability`, `condition`, `mpn`. Items zonder afbeelding, prijs of id worden overgeslagen en gelogd.

De brand van items die al een leverancier hebben (bijvoorbeeld `Sano International ltd`) blijft zoals in de bronfeed. Wil je die opschonen, voeg dan een mapping toe in `main.py`.

## 3. Resultaat eerste run (29-10-2026)

- 600 items in de feed: 556 `in stock`, 44 `out of stock`, 37 met `sale_price`, 0 overgeslagen.
- Dekking: 417 van 428 productpagina's uit de sitemap (97,4%). De 11 die ontbreken zijn terecht: 5 servicepagina's (wijnkistjes, inpakservice, wenskaart; geven 404), 5 Sonomask-producten zonder afbeelding en `moraz-polygonum-dagcreme-nd` (HTTP 500).
- Alle 417 SKU's van de standaardvarianten staan in de feed. Dat is de waarde die ViewContent meestuurt.

### Kwaliteitspoort
De run faalt (en de vorige feed blijft staan) bij minder dan 300 items, minder dan 85% dekking of meer dan 60% out of stock. Dat vangt een kapotte of gewijzigde bronfeed op in plaats van stil een lege feed te leveren.

## 4. Pixel-audit

Gecontroleerd op de live site en in de GTM-container `GTM-N3HQT67` (alleen lezen):

- Meta-pixel **`929079074149829`** ("Pixel van IPC advertentie") staat client-side in GTM en is actief. Tussen 14 en 29 okt: 15.596 ViewContent, 2.488 AddToCart, 514 Purchase.
- `content_type` = **`product`**. `content_ids` = `item_id.toString()`, dus een array van strings. Dat past bij een products-catalogus.
- `item_id` in de dataLayer is de **variant-SKU** (string, voorloopnullen blijven, bijvoorbeeld `"09487"`). Geen prefix.
- **ViewContent stuurt altijd de standaardvariant.** Bij `?sku=67011` en na een variantwissel blijft `view_item` op `67002` (de eerste variant). Met `item_group_id` in de feed groepeert Meta de varianten, dus dit is goed op te vangen.
- Niets in de tracking is aangepast.

## 5. Punten voor de webbouwer

1. **JSON-LD zegt overal `InStock`**, ook bij uitverkochte producten. Dit is de waarschijnlijke oorzaak van de melding "Niet-overeenkomende productbeschikbaarheid" in Merchant Center (nu 15 producten niet goedgekeurd, 549 beperkt). De structured data moet per variant kloppen.
2. **SKU's met rare tekens:** `75226..`, `75227..`, `75228..` (punten) en `32109 `, `33033 ` (spatie erachter). De feed bevat de opgeschoonde variant voor de laatste twee, de pixel kan de ruwe waarde sturen.
3. **Dubbele Moraz-producten:** de oude losse producten en het nieuwe gegroepeerde product `moraz` delen dezelfde SKU's.
4. **Productpagina's die 404/500 geven** staan wel in de sitemap (zie hierboven).

## 6. Meta koppelen (Commerce Manager)

1. **Commerce Manager > Catalogs > Create catalog > E-commerce (Producten)**, naam `IPC Israelwinkel`. Koppel aan het IPC-account (ad account `346639865892094`).
2. **Data sources > Add items > Data feed > Scheduled feed**. URL: de raw-URL hierboven. Frequentie dagelijks, een uur na de GitHub-run (bv. 07:00 NL), valuta EUR.
3. **Koppel de pixel** `929079074149829` (Catalog > Settings > Connected data sources).
4. **Product sets** maken op `custom_label_0` (hoofdcategorie), `custom_label_1` (aanbieding) of `custom_label_2` (prijsklasse). Zet een set "In stock" via de `availability`-filter.
5. Controleer na ongeveer 7 dagen **Events Manager > Diagnostics > Catalog match rate** (streef 80 tot 90% of hoger). Daarna Advantage+ catalog ads inzetten.

Checklist voor de launch:
- [ ] Feed laadt zonder fouten (Data sources > Diagnostics)
- [ ] Brand is gevuld bij alle items
- [ ] Pixel gekoppeld aan de catalogus
- [ ] `content_ids` in het event zijn strings en gelijk aan de feed-`id`
- [ ] Match rate gecontroleerd na enkele dagen

## 7. Monitoring

1. **Status-badge** bovenaan: groen = laatste run geslaagd, rood = gefaald.
2. **Actions-tab** van de repo: elke run met datum en vinkje.
3. **`output/last_run.txt`** verandert elke run (tijdstip, aantallen, dekking), dus er is altijd een dagelijkse commit. Dat houdt ook de geplande workflow actief (GitHub zet cron-workflows uit na 60 dagen zonder activiteit).
4. **E-mail bij falen:** GitHub mailt de repo-eigenaar automatisch.
5. **Meta-kant:** de data source toont "Last updated" en fetch-fouten.

Snelle check: `gh run list --workflow=daily-feed.yml --limit 5`.

## 8. Lokaal draaien

```bash
python3 -m unittest discover -s tests
python3 main.py
# resultaat in output/
```

Optionele Google Sheet-mirror: zet `GOOGLE_SERVICE_ACCOUNT_JSON` en `SPREADSHEET_ID` als GitHub-secrets en deel de Sheet met het service-account (tab `IPC`, waarden als tekst zodat `09487` zijn voorloopnul houdt).

## 9. Onderhoud

- **Nieuwe hoofdcategorie in het menu?** Voeg hem toe aan `TOP_LEVEL` (en eventueel `CATEGORY_ALIASES`) in `main.py`.
- **Bronfeed verhuist of wijzigt?** De run faalt met een duidelijke melding. Pas `FEED_URL` aan.
- **Wil je de leveranciersnaam als brand opschonen?** Voeg een mapping toe in `transform()`.
