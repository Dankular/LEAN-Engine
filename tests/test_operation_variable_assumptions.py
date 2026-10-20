import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app


class OperationVariableAssumptionTests(unittest.TestCase):
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

    def test_registry_publishes_dynamic_operation_variable_selector(self) -> None:
        response = self.client.get("/v2/assumptions", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        registry = response.json()
        self.assertEqual(registry["registry_version"], "2.9.0")
        scheme = next(
            item for item in registry["dynamic_assumptions"]
            if item["type"] == "OperationVariable"
        )
        self.assertEqual(scheme["id_pattern"], "operation:variable:{identifier}")
        self.assertEqual(scheme["selection_policy"], "caller_override_only")
        formula_variable_scheme = next(
            item for item in registry["dynamic_assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(
            formula_variable_scheme["id_pattern"],
            "formula:variable:{identifier}={value}",
        )
        self.assertEqual(formula_variable_scheme["entry_version"], 2)

        capabilities = self.client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.assertIn(
            "OperationVariable",
            capabilities.json()["interpretations"]["assumption_types"],
        )
        self.assertIn(
            "FormulaVariable",
            capabilities.json()["interpretations"]["assumption_types"],
        )

    def test_formula_variable_supplies_value_to_expression_and_lean_checks_result(self) -> None:
        unassigned = self.client.get(
            "/v2/query",
            params={"input": "x+y", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(unassigned.status_code, 200, unassigned.text)
        unassigned_result = unassigned.json()["queryresult"]
        variables = [
            item for item in unassigned_result["assumptions"]
            if item["type"] == "FormulaVariable"
        ]
        self.assertEqual(len(variables), 2)
        self.assertTrue(all(item["selected"] is None for item in variables))
        self.assertEqual(
            {item["value_input_pattern"] for item in variables},
            {"formula:variable:x=<value>", "formula:variable:y=<value>"},
        )

        assigned = self.client.get(
            "/v2/query",
            params={
                "input": "x^2+2*x+1",
                "assumption": "formula:variable:x=3",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(assigned.status_code, 200, assigned.text)
        result = assigned.json()["queryresult"]
        self.assertTrue(result["success"], result)
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(assumption["selected"], "formula:variable:x=3")
        self.assertEqual(assumption["selection_source"], "caller_override")
        verification = result["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])

    def test_validatequery_applies_formula_variable_value_without_computing(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "x^2+1",
                "assumption": "formula:variable:x=2",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["validatequeryresult"]
        self.assertTrue(result["success"], result)
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(assumption["selected"], "formula:variable:x=2")
        self.assertEqual(
            result["interpretation"]["formula_variable_assignments"],
            {"x": "2"},
        )

    def test_formula_variable_rejects_unknown_symbols_and_free_variable_values(self) -> None:
        unknown = self.client.get(
            "/v2/query",
            params={
                "input": "x+1",
                "assumption": "formula:variable:z=2",
                "output": "json",
            },
            headers=self.headers,
        )
        self.assertEqual(unknown.status_code, 400, unknown.text)
        self.assertIn("not present", unknown.json()["queryresult"]["error_detail"]["msg"])

        nonconstant_value = self.client.get(
            "/v2/query",
            params={
                "input": "x+1",
                "assumption": "formula:variable:x=y+1",
                "output": "json",
            },
            headers=self.headers,
        )
        self.assertEqual(nonconstant_value.status_code, 400, nonconstant_value.text)
        self.assertIn(
            "must not contain free variables",
            nonconstant_value.json()["queryresult"]["error_detail"]["msg"],
        )

    def test_formula_variable_accepts_compound_unit_values_and_reports_proof_limit(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "x+x",
                "assumption": "formula:variable:x=6.5 m/s",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        self.assertTrue(result["success"], result)
        input_pod = next(item for item in result["pods"] if item["id"] == "Input")
        result_pod = next(item for item in result["pods"] if item["id"] == "Result")
        self.assertEqual(
            input_pod["subpods"][0]["plaintext"],
            "(6.5 m/s)+(6.5 m/s)",
        )
        self.assertEqual(result_pod["subpods"][0]["plaintext"], "13*meters/seconds")
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        )
        self.assertEqual(
            assumption["selected"],
            "formula:variable:x=6.5 m/s",
        )
        self.assertEqual(
            assumption["values"][0]["normalized_value"],
            "(6.5)*(unit_meter/unit_second)",
        )
        verification = result["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "unsupported", verification)
        self.assertFalse(verification["verified"])
        self.assertIn("bounded rational polynomial", verification["reason"])

    def test_formula_variable_unit_values_are_dimension_checked(self) -> None:
        incompatible = self.client.get(
            "/v2/query",
            params={
                "input": "x+1",
                "assumption": "formula:variable:x=6.5 m/s",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(incompatible.status_code, 200, incompatible.text)
        result = incompatible.json()["queryresult"]
        self.assertFalse(result["success"], result)
        self.assertIn(
            "incompatible physical dimensions",
            result["tips"][0]["text"],
        )

    def test_formula_variable_rejects_unknown_unit_aliases(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "x",
                "assumption": "formula:variable:x=6.5 f/s",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn(
            "not a supported expression",
            response.json()["queryresult"]["error_detail"]["msg"],
        )

    def test_physical_unit_symbols_are_not_offered_as_formula_variables(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "x*10 meters", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        self.assertTrue(result["success"], result)
        formula_variables = [
            item for item in result["assumptions"]
            if item["type"] == "FormulaVariable"
        ]
        self.assertEqual(len(formula_variables), 1)
        self.assertEqual(
            formula_variables[0]["value_input_pattern"],
            "formula:variable:x=<value>",
        )

    def test_validatequery_accepts_unit_formula_variable_values(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "x^2",
                "assumption": "formula:variable:x=6.5 m/s",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["validatequeryresult"]
        self.assertTrue(result["success"], result)
        self.assertEqual(
            result["interpretation"]["formula_variable_assignments"]["x"],
            "(6.5)*(unit_meter/unit_second)",
        )

    def test_validatequery_accepts_exact_fraction_unit_magnitudes(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "x^2",
                "assumption": "formula:variable:x=1/2 m/s",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["validatequeryresult"]
        self.assertTrue(result["success"], result)
        self.assertEqual(
            result["interpretation"]["formula_variable_assignments"]["x"],
            "(1/2)*(unit_meter/unit_second)",
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
            if item["type"] == "OperationVariable"
        )
        self.assertIsNone(assumption["selected"])
        self.assertEqual(assumption["selection_source"], "needs_clarification")
        self.assertEqual(
            [item["input"] for item in assumption["values"]],
            ["operation:variable:x", "operation:variable:y"],
        )
        self.assertFalse(result["classification"]["selection_authority"])

    def test_caller_variable_selection_routes_to_lean_checked_derivative(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "differentiate x*y",
                "assumption": "operation:variable:y",
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
            if item["type"] == "OperationVariable"
        )
        self.assertEqual(assumption["selected"], "operation:variable:y")
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
            if item["type"] == "OperationVariable"
        )
        self.assertIsNone(assumption["selected"])

        selected = self.client.get(
            "/v2/validatequery",
            params={
                "input": "differentiate x*y",
                "assumption": "operation:variable:y",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(selected.status_code, 200, selected.text)
        selected_result = selected.json()["validatequeryresult"]
        self.assertTrue(selected_result["success"], selected_result)
        selected_assumption = next(
            item for item in selected_result["assumptions"]
            if item["type"] == "OperationVariable"
        )
        self.assertEqual(selected_assumption["selected"], "operation:variable:y")
        self.assertEqual(
            selected_result["interpretation"]["natural_language_plan"]["selected_candidate"]["variable"],
            "y",
        )

    def test_variable_outside_current_formula_candidates_is_rejected(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "differentiate x*y",
                "assumption": "operation:variable:z",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("candidate", response.json()["queryresult"]["error_detail"]["msg"])


if __name__ == "__main__":
    unittest.main()
