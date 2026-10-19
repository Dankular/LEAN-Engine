import unittest

from leanproof_service.computation import check_computation, prepare_computation


class ExactPiTrigonometricConstantTests(unittest.TestCase):
    def test_standard_exact_sine_cosine_and_tangent_values(self) -> None:
        cases = {
            "Sin[Pi]": "0",
            "Sin[-Pi]": "0",
            "Sin[2Pi]": "0",
            "Cos[2Pi]": "1",
            "Tan[2Pi]": "0",
            "Sin[Pi/2]": "1",
            "Sin[-Pi/2]": "-1",
            "Sin[Pi/3]": "sqrt(3)/2",
            "Sin[Pi/4]": "sqrt(2)/2",
            "Sin[Pi/6]": "1/2",
            "Sin[Pi/5]": "sqrt(10 - 2*sqrt(5))/4",
            "Sin[Pi/10]": "(sqrt(5) - 1)/4",
            "Sin[Pi/8]": "sqrt(2 - sqrt(2))/2",
            "Sin[Pi/16]": "sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Sin[Pi/32]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Sin[Pi/64]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2)))))/2",
            "Sin[Pi/128]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))))/2",
            "Sin[3Pi/8]": "sqrt(2 + sqrt(2))/2",
            "Cos[3Pi/8]": "sqrt(2 - sqrt(2))/2",
            "Sin[7Pi/16]": "sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Cos[7Pi/16]": "sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Sin[15Pi/32]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Cos[15Pi/32]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Sin[5Pi/8]": "sqrt(2 + sqrt(2))/2",
            "Cos[5Pi/8]": "-sqrt(2 - sqrt(2))/2",
            "Sin[2Pi/5]": "sqrt(10 + 2*sqrt(5))/4",
            "Cos[2Pi/5]": "(sqrt(5) - 1)/4",
            "Sin[3Pi/10]": "(1 + sqrt(5))/4",
            "Cos[3Pi/10]": "sqrt(10 - 2*sqrt(5))/4",
            "Sin[31Pi/64]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2)))))/2",
            "Cos[63Pi/128]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))))/2",
            "Cos[Pi]": "-1",
            "Cos[Pi/2]": "0",
            "Cos[Pi/3]": "1/2",
            "Cos[Pi/4]": "sqrt(2)/2",
            "Cos[Pi/6]": "sqrt(3)/2",
            "Cos[Pi/5]": "(1 + sqrt(5))/4",
            "Cos[Pi/10]": "sqrt(10 + 2*sqrt(5))/4",
            "Cos[Pi/8]": "sqrt(2 + sqrt(2))/2",
            "Cos[Pi/16]": "sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Cos[Pi/32]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Cos[Pi/64]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2)))))/2",
            "Cos[Pi/128]": "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))))/2",
            "Tan[Pi]": "0",
            "Tan[Pi/3]": "sqrt(3)",
            "Tan[Pi/4]": "1",
            "Tan[Pi/6]": "1/sqrt(3)",
            "Sin[2Pi/3]": "sqrt(3)/2",
            "Cos[2 Pi/3]": "-1/2",
            "Tan[2*Pi/3]": "-sqrt(3)",
            "Sin[5Pi/6]": "1/2",
            "Cos[5Pi/6]": "-sqrt(3)/2",
            "Tan[5Pi/6]": "-1/sqrt(3)",
            "Sin[-2Pi/3]": "-sqrt(3)/2",
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
            "Sin[Pi/5]",
            "Sin[Pi/10]",
            "Cos[Pi/10]",
            "Sin[2Pi/3]",
            "Sin[2Pi/5]",
            "Cos[2Pi/3]",
            "Tan[2Pi/3]",
            "Sin[2Pi]",
            "Sin[3Pi/8]",
            "Cos[3Pi/8]",
            "Cos[5Pi/8]",
            "Cos[3Pi/10]",
            "Sin[Pi/64]",
            "Cos[Pi/128]",
            "Sin[31Pi/64]",
            "Cos[63Pi/128]",
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
        self.assertIsNone(prepare_computation("Sin[Pi/7]", 10))
        self.assertIsNone(prepare_computation("Sin[3Pi/16]", 10))
        self.assertIsNone(prepare_computation("Tan[3Pi/8]", 10))
        self.assertIsNone(prepare_computation("Sin[Pi/8192]", 10))
        self.assertIsNone(prepare_computation("Cos[Pi/8192]", 10))
        self.assertIsNone(prepare_computation("Tan[Pi/2]", 10))
        self.assertIsNone(prepare_computation("Sin[pi/6]", 10))


if __name__ == "__main__":
    unittest.main()
