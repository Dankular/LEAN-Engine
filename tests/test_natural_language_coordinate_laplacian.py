import unittest

from leanproof_service.natural_language_query import (
    NaturalLanguageClarification,
    plan_natural_language_operation,
)


class NaturalLanguageCoordinateLaplacianTests(unittest.TestCase):
    def test_laplacian_phrase_produces_scalar_operation(self) -> None:
        plan = plan_natural_language_operation(
            "Find the Laplacian of x^2 + y^2 + z^2"
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.operation, "coordinate_laplacian")
        self.assertEqual(plan.expression, "x^2 + y^2 + z^2")
        self.assertEqual(plan.rule_id, "coordinate_laplacian_scalar")
        self.assertIsNone(plan.coordinate_system)

    def test_lap_synonym_and_math_phrase_normalization(self) -> None:
        plan = plan_natural_language_operation(
            "Compute the lap of x squared plus y squared using Cartesian coordinates"
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.operation, "coordinate_laplacian")
        self.assertEqual(plan.expression, "(x)**2 + (y)**2")
        self.assertEqual(plan.coordinate_system, "Cartesian3D")
        self.assertEqual(plan.rule_id, "coordinate_laplacian_scalar")

    def test_optional_chart_names_are_preserved(self) -> None:
        cases = (
            ("in Cartesian coordinates", "Cartesian3D"),
            ("in cylindrical coordinates", "Cylindrical3D"),
            ("in spherical coordinates", "Spherical3D"),
        )
        for suffix, expected_chart in cases:
            with self.subTest(chart=suffix):
                plan = plan_natural_language_operation(
                    f"Find the Laplacian of x*y {suffix}"
                )
                self.assertIsNotNone(plan)
                assert plan is not None
                self.assertEqual(plan.operation, "coordinate_laplacian")
                self.assertEqual(plan.coordinate_system, expected_chart)

    def test_unsupported_chart_and_missing_field_clarify(self) -> None:
        with self.assertRaisesRegex(NaturalLanguageClarification, "unsupported"):
            plan_natural_language_operation(
                "Find the Laplacian of x^2 in polar coordinates"
            )
        with self.assertRaisesRegex(NaturalLanguageClarification, "scalar field"):
            plan_natural_language_operation("Find the Laplacian of")


if __name__ == "__main__":
    unittest.main()
