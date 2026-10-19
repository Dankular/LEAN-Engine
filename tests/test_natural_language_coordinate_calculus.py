import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.natural_language_query import (
    NaturalLanguageClarification,
    plan_natural_language_operation,
)


class NaturalLanguageCoordinateCalculusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["API_BEARER_TOKEN"] = "natural-coordinate-test-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer natural-coordinate-test-token"}

    def test_gradient_plan_preserves_explicit_chart(self) -> None:
        plan = plan_natural_language_operation(
            "Find the gradient of x squared plus y squared in cylindrical coordinates"
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.operation, "coordinate_gradient")
        self.assertEqual(plan.expression, "(x)**2 + (y)**2")
        self.assertEqual(plan.coordinate_system, "Cylindrical3D")
        self.assertEqual(plan.rule_id, "coordinate_gradient_field")

    def test_divergence_and_curl_plans_require_three_explicit_components(self) -> None:
        divergence = plan_natural_language_operation(
            "Calculate the divergence of vector field F=(x, y, z)"
        )
        curl = plan_natural_language_operation(
            "Find the curl of (0, 0, x*y) using Cartesian coordinates"
        )

        self.assertIsNotNone(divergence)
        assert divergence is not None
        self.assertEqual(divergence.operation, "coordinate_divergence")
        self.assertEqual(divergence.components, ("x", "y", "z"))
        self.assertIsNone(divergence.coordinate_system)
        self.assertIsNotNone(curl)
        assert curl is not None
        self.assertEqual(curl.operation, "coordinate_curl")
        self.assertEqual(curl.components, ("0", "0", "x*y"))
        self.assertEqual(curl.coordinate_system, "Cartesian3D")

    def test_unsupported_chart_and_incomplete_vector_request_clarify(self) -> None:
        with self.assertRaisesRegex(NaturalLanguageClarification, "unsupported"):
            plan_natural_language_operation(
                "Find the gradient of x in polar coordinates"
            )
        with self.assertRaisesRegex(NaturalLanguageClarification, "exactly three"):
            plan_natural_language_operation(
                "Find the divergence of the vector field (x, y)"
            )

    def test_validatequery_exposes_natural_language_coordinate_plan(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "Find the divergence of (sin(x), cos(y), 0) in spherical coordinates",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["validatequeryresult"]
        typed_ast = result["interpretation"]["typed_ast"]
        self.assertEqual(typed_ast["node"], "vector_calculus_operation")
        self.assertEqual(typed_ast["operation"], "divergence")
        self.assertEqual(typed_ast["coordinate_system"], "Spherical3D")
        plan = result["interpretation"]["natural_language_plan"]
        self.assertEqual(plan["rule_id"], "coordinate_divergence_components")
        self.assertEqual(
            plan["selected_candidate"]["components"], ["sin(x)", "cos(y)", "0"]
        )
        self.assertEqual(
            plan["semantic_fidelity"],
            "english_to_formula_correspondence_unverified",
        )
        self.assertFalse(result["classification"]["query_intent"]["model_ranked"])

    def test_natural_language_coordinate_operations_are_lean_checked(self) -> None:
        cases = [
            (
                "Find the gradient of x squared plus y squared in Cartesian coordinates",
                "coordinate_gradient",
                "(2*x, 2*y, 0)",
            ),
            (
                "Calculate the divergence of (x, y, z)",
                "coordinate_divergence",
                "3",
            ),
            (
                "Find the curl of (0, 0, x*y) in Cartesian coordinates",
                "coordinate_curl",
                "(x, -y, 0)",
            ),
        ]
        for input_text, claim_type, expected in cases:
            response = self.client.get(
                "/v2/query",
                params={"input": input_text, "verify": "true", "output": "json"},
                headers=self.headers,
            )

            with self.subTest(input=input_text):
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["queryresult"]
                plan = result["interpretation"]["natural_language_plan"]
                self.assertEqual(plan["selection_source"], "deterministic_template")
                self.assertEqual(
                    plan["selected_candidate"]["assumption_id"],
                    "coordinate:cartesian-3d",
                )
                verification = result["evidence"]["formal_verification"]
                self.assertEqual(verification["status"], "checked", verification)
                self.assertEqual(verification["claim_type"], claim_type)
                self.assertTrue(verification["verified"])
                self.assertEqual(verification["axiom_audit"]["status"], "passed")
                result_pod = next(pod for pod in result["pods"] if pod["id"] == "Result")
                self.assertEqual(result_pod["subpods"][0]["plaintext"], expected)


if __name__ == "__main__":
    unittest.main()
