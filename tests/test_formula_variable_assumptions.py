import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app


class FormulaVariableAssumptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "formula-variable-assumption-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer formula-variable-assumption-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_registry_publishes_dynamic_formula_variable_selector(self) -> None:
        response = self.client.get("/v2/assumptions", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        registry = response.json()
        self.assertEqual(registry["registry_version"], "2.7.0")
        scheme = next(
            item for item in registry["dynamic_assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(scheme["id_pattern"], "formula:variable:{identifier}")
        self.assertEqual(scheme["selection_policy"], "caller_override_only")

        capabilities = self.client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.assertIn(
            "FormulaVariable",
            capabilities.json()["interpretations"]["assumption_types"],
        )

    def test_ambiguous_natural_language_operation_returns_variable_candidates(self) -> None:
        with patch(
            "leanproof_service.full_results.run_symbolic",
            side_effect=AssertionError("an unresolved variable must not reach the solver"),
        ):
            response = self.client.get(
                "/v2/query",
                params={"input": "differentiate x*y", "output": "json"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        self.assertFalse(result["success"])
        self.assertFalse(result["error"])
        self.assertEqual(result["pods"], [])
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertIsNone(assumption["selected"])
        self.assertEqual(assumption["selection_source"], "needs_clarification")
        self.assertEqual(
            [item["input"] for item in assumption["values"]],
            ["formula:variable:x", "formula:variable:y"],
        )
        self.assertFalse(result["classification"]["selection_authority"])

    def test_caller_variable_selection_routes_to_lean_checked_derivative(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "differentiate x*y",
                "assumption": "formula:variable:y",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        self.assertTrue(result["success"], result)
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(assumption["selected"], "formula:variable:y")
        self.assertEqual(assumption["selection_source"], "caller_override")
        self.assertEqual(
            result["interpretation"]["natural_language_plan"]["variable_source"],
            "caller_assumption",
        )
        verification = result["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_validatequery_returns_and_applies_variable_choices_without_computing(self) -> None:
        unresolved = self.client.get(
            "/v2/validatequery",
            params={"input": "differentiate x*y", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(unresolved.status_code, 200, unresolved.text)
        unresolved_result = unresolved.json()["validatequeryresult"]
        assumption = next(
            item for item in unresolved_result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertIsNone(assumption["selected"])

        selected = self.client.get(
            "/v2/validatequery",
            params={
                "input": "differentiate x*y",
                "assumption": "formula:variable:y",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(selected.status_code, 200, selected.text)
        selected_result = selected.json()["validatequeryresult"]
        self.assertTrue(selected_result["success"], selected_result)
        selected_assumption = next(
            item for item in selected_result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(selected_assumption["selected"], "formula:variable:y")
        self.assertEqual(
            selected_result["interpretation"]["natural_language_plan"]["selected_candidate"]["variable"],
            "y",
        )

    def test_variable_outside_current_formula_candidates_is_rejected(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "differentiate x*y",
                "assumption": "formula:variable:z",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("candidate", response.json()["queryresult"]["error_detail"]["msg"])


if __name__ == "__main__":
    unittest.main()
