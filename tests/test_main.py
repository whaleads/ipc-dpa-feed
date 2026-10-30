import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_strips_supplier_prefix(self):
        self.assertEqual(main.clean_title("Beth-El food ltd Jam - Kersen", "Beth-El food ltd"), "Jam - Kersen")

    def test_keeps_title_without_prefix(self):
        self.assertEqual(main.clean_title("Herder olijfhout", ""), "Herder olijfhout")

    def test_strips_trailing_sku(self):
        t = main.clean_title("Tivit Barbary-bites - 63151", "Tivit", "63151")
        self.assertEqual(t, "Barbary-bites")

    def test_collapses_repeated_variant_name(self):
        t = main.clean_title("Ben Yehuda Ltd Hanger chai verguld - Hanger Chai verguld", "Ben Yehuda Ltd")
        self.assertEqual(t, "Hanger chai verguld")

    def test_never_returns_empty(self):
        self.assertEqual(main.clean_title("Acme", "Acme"), "Acme")


class CategoryTests(unittest.TestCase):
    def test_top_level_first(self):
        main_cat, h = main.build_hierarchy(["ontbijt", "jam", "Voedsel & Keuken"])
        self.assertEqual(main_cat, "Voedsel & Keuken")
        self.assertEqual(h, "Voedsel & Keuken > Ontbijt > Jam")

    def test_case_normalised(self):
        self.assertEqual(main.build_hierarchy(["boeken", "studie"])[1], "Boeken > Studie")

    def test_no_top_level_uses_first(self):
        self.assertEqual(main.build_hierarchy(["Studie", "kinderboeken"])[0], "Studie")

    def test_empty(self):
        self.assertEqual(main.build_hierarchy([]), ("Overig", "Overig"))

    def test_alias_maps_to_top_level_and_dedupes(self):
        main_cat, h = main.build_hierarchy(["Eten & Ingrediënten", "kruidenmixen"])
        self.assertEqual(main_cat, "Voedsel & Keuken")
        self.assertEqual(h, "Voedsel & Keuken > Kruidenmixen")


class TransformTests(unittest.TestCase):
    def item(self, **kw):
        base = {
            "id": "09487", "group": "prod_1", "title": "Proefpakket", "description": "Tekst",
            "link": "https://www.israelwinkel.nl/producten/proefpakket", "image": "https://x/y.jpg",
            "additional": [], "availability": "in stock", "price": "79.50 EUR",
            "sale_price": "69.50 EUR", "brand": "", "mpn": "09487", "product_types": ["wijn"],
        }
        base.update(kw)
        return base

    def stats(self):
        return {"duplicate_ids": [], "ids_resolved": [], "brand_fallback": 0,
                "titles_cleaned": 0, "description_fallback": 0}

    def test_brand_fallback_and_id_stays_string(self):
        rows, skipped = main.transform([self.item()], self.stats())
        self.assertEqual(rows[0]["brand"], main.BRAND_FALLBACK)
        self.assertEqual(rows[0]["id"], "09487")
        self.assertEqual(skipped, [])

    def test_sale_labels(self):
        rows, _ = main.transform([self.item()], self.stats())
        self.assertEqual(rows[0]["custom_label_1"], "aanbieding")
        self.assertEqual(rows[0]["custom_number_0"], "69.50")
        self.assertEqual(rows[0]["custom_label_2"], "30 tot 75 euro")

    def test_sale_price_not_lower_is_dropped(self):
        rows, _ = main.transform([self.item(sale_price="99.00 EUR")], self.stats())
        self.assertEqual(rows[0]["sale_price"], "")

    def test_skips_items_without_image(self):
        rows, skipped = main.transform([self.item(image="")], self.stats())
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0][2], "geen link of afbeelding")

    def test_empty_description_gets_fallback(self):
        rows, _ = main.transform([self.item(description="")], self.stats())
        self.assertIn("Israelwinkel.nl", rows[0]["description"])


class XmlTests(unittest.TestCase):
    def test_escapes_and_strips_control_chars(self):
        rows, _ = main.transform([TransformTests().item(title="A & B\x0b", description="x < y")],
                                 TransformTests().stats())
        xml = main.rss_item(rows[0])
        self.assertIn("A &amp; B", xml)
        self.assertNotIn("\x0b", xml)
        self.assertIn("x &lt; y", xml)


if __name__ == "__main__":
    unittest.main()
