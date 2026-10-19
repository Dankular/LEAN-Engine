import unittest

from leanproof_service.computation import prepare_computation
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


if __name__ == "__main__":
    unittest.main()
