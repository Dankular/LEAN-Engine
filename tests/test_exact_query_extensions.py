import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.descriptive_statistics import StatisticsError, evaluate_statistics
from leanproof_service.natural_language_query import plan_natural_language_operation
from leanproof_service.unit_conversion import UnitConversionError, evaluate_unit_conversion


def _text(evaluator, query: str) -> str:
    outcome = evaluator(query)
    assert outcome is not None, query
    return outcome.result_text


class UnitConversionTests(unittest.TestCase):
    def test_exact_conversions(self) -> None:
        cases = {
            "how many feet in 2 miles": "10560 feet",
            "convert 2.5 hours to minutes": "150 minutes",
            "10 m/s to km/h": "36 km/h",
            "60 mph to km/h": "96.56064 km/h",
            "1 cup to tablespoons": "16 tablespoons",
            "-40 C to F": "-40 degrees Fahrenheit",
            "100 F to C": "≈ 37.77777778 degrees Celsius",
            "5 km to miles": "≈ 3.106855961 miles",
            "1 degree to radians": "pi/180 radians",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(_text(evaluate_unit_conversion, query), expected)

    def test_unknown_units_do_not_match_and_mismatches_fail(self) -> None:
        self.assertIsNone(evaluate_unit_conversion("3 x to y"))
        with self.assertRaises(UnitConversionError):
            evaluate_unit_conversion("5 kg to meters")
        with self.assertRaises(UnitConversionError):
            evaluate_unit_conversion("-300 C to K")


class StatisticsTests(unittest.TestCase):
    def test_statistics(self) -> None:
        cases = {
            "mean of 1,2,3,4": "2.5",
            "median of 3,1,2": "2",
            "mode of 1,2,2,3": "{2}",
            "variance of 1,2,3,4": "5/3",
            "standard deviation of 2,4,4,4,5,5,7,9": "4*sqrt(14)/7",
            "std dev of {1,2,3}": "1",
            "range of 1 5 9": "8",
            "geometric mean of 1,2,4": "2",
            "average of 1/2, 3/4": "0.625",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(_text(evaluate_statistics, query), expected)

    def test_non_numeric_lists_do_not_match_and_bad_inputs_fail(self) -> None:
        self.assertIsNone(evaluate_statistics("mean of x,y"))
        self.assertIsNone(evaluate_statistics("sum of 1/n^2 from 1 to infinity"))
        with self.assertRaises(StatisticsError):
            evaluate_statistics("variance of 5")
        with self.assertRaises(StatisticsError):
            evaluate_statistics("geometric mean of -1, 2")


class SymbolicPlannerTests(unittest.TestCase):
    def test_partial_fraction_plan_infers_variable(self) -> None:
        plan = plan_natural_language_operation("partial fractions of 1/(x^2-1)")
        assert plan is not None
        self.assertEqual((plan.operation, plan.variable), ("apart", "x"))

    def test_series_defaults_to_point_zero_order_six(self) -> None:
        plan = plan_natural_language_operation("series of sin(x)")
        assert plan is not None
        self.assertEqual((plan.operation, plan.point, plan.order), ("series", "0", 6))

    def test_system_plan_collects_equations_and_variables(self) -> None:
        plan = plan_natural_language_operation("solve x^2+y^2=1 and x+y=1 for x, y")
        assert plan is not None
        self.assertEqual(plan.operation, "solve_system")
        self.assertEqual(plan.variables, ("x", "y"))
        self.assertEqual(len(plan.equations or ()), 2)

    def test_single_equation_still_uses_solve(self) -> None:
        plan = plan_natural_language_operation("solve x^2=4")
        assert plan is not None
        self.assertEqual(plan.operation, "solve")


class QueryTests(unittest.TestCase):
    def result(self, text: str, **params: str) -> dict:
        token = "exact-extension-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={"input": text, "output": "json", **params},
                    headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["queryresult"]

    def primary(self, queryresult: dict) -> str:
        pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        return pod["subpods"][0]["plaintext"]

    def test_symbolic_extensions_over_http(self) -> None:
        cases = {
            "partial fractions of 1/(x^2-1)": "-1/(2*(x + 1)) + 1/(2*(x - 1))",
            "apart(1/(x^2-1))": "-1/(2*(x + 1)) + 1/(2*(x - 1))",
            "series of 1/(1-x)": "1 + x + x**2 + x**3 + x**4 + x**5 + O(x**6)",
            "solve x^2+y^2=1, x+y=1": "{x = 0, y = 1}, {x = 1, y = 0}",
            "solve x+y=2, x-y=0": "{x = 1, y = 1}",
            "solve x^2+y^2=-1, x=y": "no real solutions",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.primary(self.result(query)), expected)

    def test_non_polynomial_system_is_rejected(self) -> None:
        self.assertFalse(self.result("solve sin(x)=y, x=1")["success"])

    def test_unit_and_statistics_pods_and_verify(self) -> None:
        queryresult = self.result("convert 5 km to miles", verify="true")
        pods = {pod["id"]: pod for pod in queryresult["pods"]}
        self.assertEqual(pods["Result"]["scanner"], "UnitConversion")
        self.assertIn("ExactResult", pods)
        self.assertEqual(queryresult["evidence"]["formal_verification"]["status"], "unsupported")
        stats = self.result("variance of 1,2,3,4")
        self.assertEqual(self.primary(stats), "5/3")
        self.assertIn("PopulationValue", {pod["id"] for pod in stats["pods"]})


if __name__ == "__main__":
    unittest.main()
