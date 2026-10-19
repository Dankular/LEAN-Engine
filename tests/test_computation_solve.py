import unittest

from leanproof_service.computation import check_computation, prepare_computation


class HigherDegreeRealPolynomialSolveTests(unittest.TestCase):
    def test_degree_five_rational_roots_are_checked_by_lean(self) -> None:
        computation = prepare_computation(
            "Solve[x^5 - x^4 - 7x^3 + x^2 + 6x == 0, x, Reals]",
            20,
        )

        self.assertIsNotNone(computation)
        assert computation is not None
        self.assertEqual(
            computation.display_result,
            "{{x -> -2}, {x -> -1}, {x -> 0}, {x -> 1}, {x -> 3}}",
        )
        result = check_computation(computation, 30)
        self.assertEqual(result.status, "checked", result.diagnostics)

    def test_degree_eight_rational_roots_with_multiplicity_are_checked(self) -> None:
        computation = prepare_computation(
            "Solve[(x+2)^2*(x+1)*x^2*(x-1)*(x-3)^2 == 0, x, Reals]",
            20,
        )

        self.assertIsNotNone(computation)
        assert computation is not None
        self.assertEqual(
            computation.display_result,
            "{{x -> -2}, {x -> -1}, {x -> 0}, {x -> 1}, {x -> 3}}",
        )
        result = check_computation(computation, 30)
        self.assertEqual(result.status, "checked", result.diagnostics)

    def test_degree_above_the_bounded_real_solve_limit_remains_unsupported(self) -> None:
        self.assertIsNone(
            prepare_computation("Solve[x^9 - 1 == 0, x, Reals]", 20)
        )


if __name__ == "__main__":
    unittest.main()
