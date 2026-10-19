import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.natural_language_query import (
    NaturalLanguageClarification,
    plan_natural_language_operation,
)


class NaturalLanguageIdentityTests(unittest.TestCase):
    def test_proof_templates_parse_both_sides_of_an_identity(self) -> None:
        plan = plan_natural_language_operation(
            "Prove that x plus zero is equal to x"
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.operation, "identity")
        self.assertEqual(plan.expression, "x + 0")
        self.assertEqual(plan.rhs, "x")
        self.assertEqual(plan.rule_id, "prove_identity")

    def test_multiple_equalities_request_clarification(self) -> None:
        with self.assertRaisesRegex(NaturalLanguageClarification, "one equation"):
            plan_natural_language_operation("Prove that x = y = z")

    def test_query_can_lean_check_a_natural_language_identity(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that x plus zero equals x",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        plan = queryresult["interpretation"]["natural_language_plan"]
        self.assertEqual(plan["rule_id"], "prove_identity")
        self.assertEqual(plan["selected_candidate"]["lhs"], "x + 0")
        self.assertEqual(plan["selected_candidate"]["rhs"], "x")
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "checked", formal)
        self.assertTrue(formal["verified"])
        self.assertIn("polynomial_difference_equals_result", formal["claim_type"])
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "0")
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
