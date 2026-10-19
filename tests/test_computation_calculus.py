import unittest

from leanproof_service.computation import _parse_exact_radical_basis, prepare_computation
from leanproof_service.input_syntax import parse_math_input
from leanproof_service.symbolic import SymbolicRequest, run_symbolic


class ReciprocalTrigonometryTests(unittest.TestCase):
    def test_cag_derivatives_include_required_nonzero_conditions(self) -> None:
        cases = {
            "Sec": ("sin(x)/cos(x)^2", "Cos[x] ≠ 0"),
            "Csc": ("-cos(x)/sin(x)^2", "sin(x) ≠ 0"),
            "Cot": ("-1 - cos(x)^2/sin(x)^2", "sin(x) ≠ 0"),
        }
        for function, (expected_result, expected_condition) in cases.items():
            with self.subTest(function=function):
                computation = prepare_computation(f"D[{function}[x], x]", 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertEqual(computation.domain_conditions, (expected_condition,))
                self.assertIn("HasDerivAt", computation.lean_source)
                self.assertIn("#print axioms wolfram_language_compute", computation.lean_source)

    def test_symbolic_backend_accepts_reciprocal_trig_functions(self) -> None:
        for function in ("sec", "csc", "cot"):
            with self.subTest(function=function):
                result = run_symbolic(SymbolicRequest(
                    operation="differentiate",
                    expression=f"{function}(x)",
                    variable="x",
                    time_limit_seconds=10,
                ))
                self.assertNotIn("error", result)
                self.assertIsInstance(result.get("result"), str)


class PiBoundedTrigonometricIntegralTests(unittest.TestCase):
    def test_sine_and_cosine_integrals_accept_selected_pi_bounds(self) -> None:
        cases = {
            "Integrate[Sin[x], {x, 0, Pi}]": "2",
            "Integrate[Cos[x], {x, -Pi/2, Pi/2}]": "2",
            "Integrate[Sin[2x], {x, 0, Pi}]": "0",
            "Integrate[Cos[x/2], {x, 0, Pi}]": "2",
            "Integrate[Sin[2x], {x, 0, Pi/2}]": "1",
            "Integrate[Sin[-2x], {x, 0, Pi/2}]": "-1",
            "Integrate[Cos[-4x], {x, 0, Pi/2}]": "0",
            "Integrate[Sin[x+Pi/2], {x, 0, Pi/2}]": "1",
            "Integrate[Cos[x+Pi/2], {x, 0, Pi/2}]": "-1",
            "Integrate[Cos[2x+Pi], {x, 0, Pi/2}]": "0",
            "Integrate[Sin[x-Pi/2], {x, -Pi/2, Pi/2}]": "-2",
            "Integrate[Sin[t+Pi/2], {t, 0, Pi/2}]": "1",
            "Integrate[Cos[x], {x, 0, Pi/4}]": "sqrt(2)/2",
            "Integrate[Sin[x], {x, 0, Pi/4}]": "1 - sqrt(2)/2",
            "Integrate[Sin[x], {x, -Pi/4, 0}]": "-1 + sqrt(2)/2",
            "Integrate[Sin[x+Pi/4], {x, 0, Pi/4}]": "sqrt(2)/2",
            "Integrate[Cos[x+Pi/4], {x, 0, Pi/4}]": "1 - sqrt(2)/2",
            "Integrate[Cos[x], {x, 0, Pi/8}]": "sqrt(2 - sqrt(2))/2",
            "Integrate[Sin[x], {x, 0, Pi/8}]": "1 - sqrt(2 + sqrt(2))/2",
            "Integrate[Cos[x], {x, Pi/8, Pi/4}]": "sqrt(2)/2 - sqrt(2 - sqrt(2))/2",
            "Integrate[Sin[x+Pi/8], {x, 0, Pi/8}]": "-sqrt(2)/2 + sqrt(2 + sqrt(2))/2",
            "Integrate[Cos[x+Pi/8], {x, 0, Pi/8}]": "sqrt(2)/2 - sqrt(2 - sqrt(2))/2",
            "Integrate[Sin[x], {x, -Pi/8, Pi/8}]": "0",
            "Integrate[Cos[x], {x, 0, Pi/16}]": "sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Integrate[Sin[x], {x, 0, Pi/16}]": "1 - sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Integrate[Cos[x], {x, Pi/16, Pi/8}]": "sqrt(2 - sqrt(2))/2 - sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Integrate[Sin[x+Pi/16], {x, 0, Pi/16}]": "-sqrt(2 + sqrt(2))/2 + sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Integrate[Cos[x+Pi/16], {x, 0, Pi/16}]": "sqrt(2 - sqrt(2))/2 - sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Integrate[Sin[x], {x, -Pi/16, Pi/16}]": "0",
            "Integrate[Cos[x], {x, 0, Pi/32}]": "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Integrate[Sin[x], {x, 0, Pi/32}]": "1 - sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Integrate[Cos[x], {x, Pi/32, Pi/16}]": "sqrt(2 - sqrt(2 + sqrt(2)))/2 - sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Integrate[Sin[x+Pi/32], {x, 0, Pi/32}]": "-sqrt(2 + sqrt(2 + sqrt(2)))/2 + sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Integrate[Cos[x+Pi/32], {x, 0, Pi/32}]": "sqrt(2 - sqrt(2 + sqrt(2)))/2 - sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Integrate[Sin[x], {x, -Pi/32, Pi/32}]": "0",
        }
        for code, expected_result in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertEqual(computation.domain_conditions, ())
                self.assertIn("intervalIntegral.integral_eq_sub_of_hasDerivAt", computation.lean_source)
                self.assertIn("#print axioms wolfram_language_compute", computation.lean_source)

    def test_lowercase_pi_is_not_mistaken_for_wolfram_constant(self) -> None:
        self.assertIsNone(prepare_computation("Integrate[Sin[x], {x, 0, pi}]", 10))
        self.assertIsNone(prepare_computation("Integrate[Sin[x+pi/2], {x, 0, 1}]", 10))
        self.assertIsNone(prepare_computation("Integrate[Sin[x+PI/2], {x, 0, Pi/2}]", 10))

    def test_nonzero_phase_is_not_claimed_by_the_exact_endpoint_path(self) -> None:
        self.assertIsNone(prepare_computation("Integrate[Sin[x+1], {x, 0, Pi}]", 10))

    def test_non_supported_pi_bounds_and_endpoint_phases_remain_unsupported(self) -> None:
        self.assertIsNone(prepare_computation("Integrate[Cos[x], {x, 0, Pi/3}]", 10))
        self.assertIsNone(prepare_computation("Integrate[Sin[3x], {x, 0, Pi/4}]", 10))
        self.assertIsNone(prepare_computation("Integrate[Cos[x], {x, 0, Pi/64}]", 10))
        self.assertIsNone(prepare_computation("Integrate[Sin[3x], {x, 0, Pi/32}]", 10))


class ExactRadicalBasisTests(unittest.TestCase):
    def test_parser_normalizes_nested_radical_products_and_powers(self) -> None:
        cases = {
            "sqrt(2)*sqrt(2+sqrt(2))": (0, 0, 1, 1, 0, 0, 0, 0),
            "sqrt(2+sqrt(2))*sqrt(2-sqrt(2))": (0, 1, 0, 0, 0, 0, 0, 0),
            "sqrt(2+sqrt(2))**2": (2, 1, 0, 0, 0, 0, 0, 0),
            "sqrt(2+sqrt(2+sqrt(2)))**2": (2, 0, 1, 0, 0, 0, 0, 0),
            "sqrt(2+sqrt(2+sqrt(2)))*sqrt(2-sqrt(2+sqrt(2)))": (0, 0, 0, 1, 0, 0, 0, 0),
            "sqrt(2+sqrt(2+sqrt(2+sqrt(2))))**2": (2, 0, 0, 0, 1, 0, 0, 0),
            "sqrt(2+sqrt(2+sqrt(2+sqrt(2))))*sqrt(2-sqrt(2+sqrt(2+sqrt(2))))": (0, 0, 0, 1, 0, 0, 0, 0),
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(_parse_exact_radical_basis(expression), expected)

    def test_parser_rejects_radicals_outside_the_supported_basis(self) -> None:
        self.assertIsNone(_parse_exact_radical_basis("sqrt(3)"))


class InverseTrigonometricAntiderivativeTests(unittest.TestCase):
    def test_inverse_sine_antiderivative_carries_real_domain_conditions(self) -> None:
        computation = prepare_computation("Integrate[1/Sqrt[1-x^2], x]", 10)

        self.assertIsNotNone(computation)
        assert computation is not None
        self.assertEqual(computation.display_result, "asin(x)")
        self.assertIn("-1 < x", computation.domain_conditions)
        self.assertIn("x < 1", computation.domain_conditions)
        self.assertIn("1 - x ^ 2 > 0", computation.domain_conditions)
        self.assertIn("Real.hasDerivAt_arcsin", computation.lean_source)
        self.assertIn("HasDerivAt", computation.lean_source)
        self.assertIn("#print axioms wolfram_language_compute", computation.lean_source)

    def test_inverse_sine_and_cosine_integrands_are_supported(self) -> None:
        cases = {
            "Integrate[ArcSin[x], x]": "x*asin(x) + sqrt(1 - x^2)",
            "Integrate[ArcCos[x], x]": "x*acos(x) - sqrt(1 - x^2)",
            "Integrate[ArcSin[2x], x]": "x*asin(2*x) + sqrt(1 - 4*x^2)/2",
        }
        for code, expected_result in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertIn("-1 <", " ".join(computation.domain_conditions))
                self.assertIn("< 1", " ".join(computation.domain_conditions))
                self.assertIn("HasDerivAt", computation.lean_source)


class ReciprocalTrigonometricAntiderivativeTests(unittest.TestCase):
    def test_reciprocal_trig_square_integrals_include_pole_conditions(self) -> None:
        cases = {
            "Integrate[Sec[x]^2, x]": ("sin(x)/cos(x)", "Cos[x] ≠ 0"),
            "Integrate[Csc[x]^2, x]": ("-cos(x)/sin(x)", "sin(x) ≠ 0"),
            "Integrate[Cot[x]^2, x]": ("-x - cos(x)/sin(x)", "sin(x) ≠ 0"),
        }
        for code, (expected_result, expected_condition) in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertIn(expected_condition, computation.domain_conditions)
                self.assertIn("HasDerivAt", computation.lean_source)
                self.assertIn("Real.sin_sq_add_cos_sq", computation.lean_source)


class InverseHyperbolicCalculusTests(unittest.TestCase):
    def test_inverse_hyperbolic_sine_derivative_and_integrals_are_planned(self) -> None:
        self.assertEqual(
            parse_math_input(r"\operatorname{arcsinh}(x)").expression,
            "asinh(x)",
        )
        derivative = prepare_computation("D[ArcSinh[x], x]", 10)
        self.assertIsNotNone(derivative)
        assert derivative is not None
        self.assertEqual(derivative.display_result, "1/sqrt(x^2 + 1)")
        self.assertEqual(derivative.domain_conditions, ())
        self.assertIn("SpecialFunctions.Arsinh", derivative.lean_source)
        self.assertIn(".arsinh", derivative.lean_source)

        cases = {
            "Integrate[1/Sqrt[1+x^2], x]": "asinh(x)",
            "Integrate[ArcSinh[x], x]": "x*asinh(x) - sqrt(x^2 + 1)",
        }
        for code, expected_result in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertIn("HasDerivAt", computation.lean_source)
                if code == "Integrate[1/Sqrt[1+x^2], x]":
                    self.assertIn("1 + x ^ 2 > 0", computation.domain_conditions)

    def test_inverse_hyperbolic_cosine_has_strict_real_domain_conditions(self) -> None:
        self.assertEqual(
            parse_math_input(r"\operatorname{arccosh}(x)").expression,
            "acosh(x)",
        )
        derivative = prepare_computation("D[ArcCosh[x], x]", 10)
        self.assertIsNotNone(derivative)
        assert derivative is not None
        self.assertEqual(derivative.display_result, "1/sqrt(x^2 - 1)")
        self.assertEqual(derivative.domain_conditions, ("x > 1",))
        self.assertIn("Real.hasDerivAt_arcosh", derivative.lean_source)

        cases = {
            "Integrate[1/Sqrt[x^2-1], x]": "log(Abs(x + sqrt(x^2 - 1)))",
            "Integrate[ArcCosh[x], x]": "x*acosh(x) - sqrt(x^2 - 1)",
        }
        for code, expected_result in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code, 10)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(computation.display_result, expected_result)
                self.assertIn("HasDerivAt", computation.lean_source)
                if code == "Integrate[ArcCosh[x], x]":
                    self.assertIn("x > 1", computation.domain_conditions)


if __name__ == "__main__":
    unittest.main()
