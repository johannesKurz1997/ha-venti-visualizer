from __future__ import annotations

import unittest

from scoring import absolute_humidity_gm3, guete_score, relative_humidity_from_absolute_pct


class GueteScoreTests(unittest.TestCase):
    def test_ideal_point_is_100(self):
        guete = guete_score(
            temp_c=21.0, humidity_rel_pct=45.0,
            ideal_temp=21.0, ideal_humidity_rel_pct=45.0,
            weight_temp=1.0, weight_humidity=1.0,
            sigma_temp=10.0, sigma_humidity_rel=40.0,
        )
        self.assertAlmostEqual(guete, 100.0)

    def test_exact_tolerance_boundary_is_0(self):
        # weight_humidity=0 isoliert die Temperatur-Dimension: bei genau
        # sigma_temp Abweichung ist D² = 1*1**2 + 0 = 1 -> guete = 0.
        guete = guete_score(
            temp_c=31.0, humidity_rel_pct=45.0,  # +10 °C == sigma_temp
            ideal_temp=21.0, ideal_humidity_rel_pct=45.0,
            weight_temp=1.0, weight_humidity=0.0,
            sigma_temp=10.0, sigma_humidity_rel=40.0,
        )
        self.assertAlmostEqual(guete, 0.0)

    def test_far_beyond_tolerance_clamped_to_0_not_negative(self):
        guete = guete_score(
            temp_c=100.0, humidity_rel_pct=100.0,
            ideal_temp=21.0, ideal_humidity_rel_pct=45.0,
            weight_temp=1.0, weight_humidity=1.0,
            sigma_temp=10.0, sigma_humidity_rel=40.0,
        )
        self.assertEqual(guete, 0.0)

    def test_zero_weights_fall_back_to_equal_weighting(self):
        guete = guete_score(
            temp_c=21.0, humidity_rel_pct=45.0,
            ideal_temp=21.0, ideal_humidity_rel_pct=45.0,
            weight_temp=0.0, weight_humidity=0.0,
            sigma_temp=10.0, sigma_humidity_rel=40.0,
        )
        self.assertAlmostEqual(guete, 100.0)


class RelativeHumidityInverseTests(unittest.TestCase):
    def test_roundtrip_absolute_to_relative(self):
        temp_c, rel_pct = 22.0, 50.0
        abs_gm3 = absolute_humidity_gm3(temp_c, rel_pct)
        recovered_rel_pct = relative_humidity_from_absolute_pct(temp_c, abs_gm3)
        self.assertAlmostEqual(recovered_rel_pct, rel_pct, places=6)


if __name__ == "__main__":
    unittest.main()
