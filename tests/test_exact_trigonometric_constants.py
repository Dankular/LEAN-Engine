import unittest

from leanproof_service.computation import check_computation, prepare_computation


class ExactPiTrigonometricConstantTests(unittest.TestCase):
    def test_standard_exact_sine_cosine_and_tangent_values(self) -> None:
        cases = {
            "Sin[Pi]": "0",
            "Sin[-Pi]": "0",
            "Sin[Pi/2]": "1",
            "Sin[-Pi/2]": "-1",
            "Sin[Pi/3]": "sqrt(3)/2",
            "Sin[Pi/4]": "sqrt(2)/2",
            "Sin[Pi/6]": "1/2",
            "Sin[Pi/8]": "sqrt(2 - sqrt(2))/2",
            "Sin[Pi/16]": "sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Sin[Pi/32]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Cos[Pi]": "-1",
            "Cos[Pi/2]": "0",
            "Cos[Pi/3]": "1/2",
            "Cos[Pi/4]": "sqrt(2)/2",
            "Cos[Pi/6]": "sqrt(3)/2",
            "Cos[Pi/8]": "sqrt(2 + sqrt(2))/2",
            "Cos[Pi/16]": "sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Cos[Pi/32]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Tan[Pi]": "0",
            "Tan[Pi/3]": "sqrt(3)",
            "Tan[Pi/4]": "1",
            "Tan[Pi/6]": "1/sqrt(3)",
            "Sin[-Pi/6]": "-1/2",
            "Cos[-Pi/3]": "1/2",
            "Tan[-Pi/4]": "-1",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected)

    def test_representative_values_are_checked_by_lean(self) -> None:
        cases = (
            "Sin[Pi/6]",
            "Sin[-Pi]",
            "Cos[Pi/8]",
            "Cos[-Pi/3]",
            "Sin[-Pi/16]",
            "Tan[-Pi/4]",
        )
        for code in cases:
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                result = check_computation(computation, 30)
                self.assertEqual(result.status, "checked", result.diagnostics)

    def test_unlisted_angles_and_tangent_poles_remain_unsupported(self) -> None:
        self.assertIsNone(prepare_computation("Sin[Pi/5]", 10))
        self.assertIsNone(prepare_computation("Cos[Pi/64]", 10))
        self.assertIsNone(prepare_computation("Tan[Pi/2]", 10))
        self.assertIsNone(prepare_computation("Sin[pi/6]", 10))


if __name__ == "__main__":
    unittest.main()
