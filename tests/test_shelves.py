import unittest
from io import StringIO

from ksq.shelves import parse_shelf_locations


HEADER_TAIL = "sku_code,name,shelf_number,level,bin_unit\n"


class ShelfHeaderTests(unittest.TestCase):
    def test_repeated_bom_before_first_column_is_ignored(self):
        source = StringIO(
            "\ufeff\ufeffout_item_id," + HEADER_TAIL
            + "1441,6939261900788,三金 西瓜霜润喉片,01,02,03\n"
        )

        result = parse_shelf_locations(source)

        self.assertEqual(result.entries["6939261900788"][0].out_item_id, "1441")

    def test_duplicate_column_after_bom_cleanup_is_rejected(self):
        source = StringIO("\ufeffout_item_id,out_item_id," + HEADER_TAIL)

        with self.assertRaisesRegex(ValueError, "库位表存在重复列：out_item_id"):
            parse_shelf_locations(source)

    def test_repeated_empty_trailing_columns_are_ignored(self):
        source = StringIO(
            "out_item_id,sku_code,name,shelf_number,level,bin_unit,updated_at,,,,,,,\n"
            "1441,6939261900788,三金 西瓜霜润喉片,01,02,03,2026-07-22T20:19:22.713181,,,,,,,\n"
        )

        result = parse_shelf_locations(source)

        self.assertEqual(result.entries["6939261900788"][0].location, "01-02-03")

    def test_missing_location_is_loaded_as_empty_with_warning(self):
        source = StringIO(
            "out_item_id,sku_code,name,shelf_number,level,bin_unit\n"
            "1441,6939261900788,三金 西瓜霜润喉片,,,\n"
        )

        result = parse_shelf_locations(source)

        self.assertEqual(result.entries["6939261900788"][0].location, "")
        self.assertEqual(result.mapped_row_count, 1)
        self.assertIn("第 2 行", result.missing_location_warnings[0])


if __name__ == "__main__":
    unittest.main()
