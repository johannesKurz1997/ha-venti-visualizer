from __future__ import annotations

import unittest

from main import _luften_attributes


class LuftenAttributesTests(unittest.TestCase):
    def test_no_device_class(self):
        attrs = _luften_attributes("Büro", 6.0, None)
        self.assertNotIn("device_class", attrs)

    def test_identical_keys_across_rooms(self):
        # Regressionsschutz: alle Räume müssen dieselben Attribut-Keys bekommen,
        # keine raumabhängige Sonderbehandlung (Ursprung des gemeldeten Bugs).
        rooms = ["Wohnzimmer", "Schlafzimmer", "Büro"]
        keysets = [frozenset(_luften_attributes(r, 1.0, None).keys()) for r in rooms]
        self.assertEqual(len(set(keysets)), 1)
        for keys in keysets:
            self.assertNotIn("device_class", keys)

    def test_expected_attributes_present(self):
        attrs = _luften_attributes("Wohnzimmer", 3.456, "2026-01-01T00:00:00+00:00")
        self.assertEqual(attrs["friendly_name"], "Lüften Wohnzimmer")
        self.assertEqual(attrs["delta_guete"], 3.46)
        self.assertEqual(attrs["stable_since"], "2026-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
