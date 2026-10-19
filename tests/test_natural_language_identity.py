import os
import unittest
from fractions import Fraction
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
        self.assertEqual(formal["claim_type"], "universal_identity")
        self.assertEqual(formal["claim_status"], "proved")
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "0")
        self.assertTrue(result["verified"])
        self.assertEqual(result["claim_status"], "proved")

    def test_query_refutes_an_identity_with_a_nonzero_constant_difference(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that x plus zero equals x plus one",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "checked", formal)
        self.assertTrue(formal["verified"])
        self.assertEqual(formal["claim_status"], "refuted")
        self.assertIn("left-minus-right equals -1", formal["claim_basis"])
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "-1")
        self.assertEqual(result["claim_status"], "refuted")
        self.assertIn("refutes", queryresult["qualification"])

    def test_query_refutes_variable_identity_with_validated_rational_witness(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that x times x equals x",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "checked", formal)
        self.assertEqual(formal["claim_status"], "refuted")
        counterexample = formal["counterexample"]
        self.assertEqual(counterexample["solver"], "z3")
        self.assertEqual(counterexample["validation"], "independent_exact_rational_replay")
        witness_x = Fraction(counterexample["assignments"]["x"])
        self.assertNotEqual(witness_x * witness_x, witness_x)
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["counterexample"], counterexample)
        self.assertIn("counterexample", queryresult["qualification"])

    def test_query_proves_supported_exact_trigonometric_identity(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that sin(pi/6) equals 1/2",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "checked", formal)
        self.assertEqual(formal["claim_status"], "proved")
        self.assertIn("Real.sin", formal["lean_source"])
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "0")
        self.assertEqual(result["claim_status"], "proved")

    def test_query_refutes_exact_trigonometric_identity_with_certified_interval(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that sin(pi/4) equals zero",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "checked", formal)
        self.assertEqual(formal["claim_status"], "refuted")
        evidence = formal["claim_refutation_evidence"]
        self.assertEqual(evidence["kind"], "lean_checked_nonzero_interval")
        self.assertGreater(Fraction(evidence["distance_from_zero_lower_bound"]), 0)
        self.assertEqual(evidence["axiom_audit"]["status"], "passed")
        self.assertIn("wolfram_exact_trigonometric_expression", evidence["lean_source"])

    def test_unsupported_identity_is_not_reported_as_proved(self) -> None:
        token = "natural-language-identity-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={
                        "input": "Prove that sin(x) equals cos(x)",
                        "verify": "true",
                        "output": "json",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "unsupported")
        self.assertFalse(formal["verified"])
        self.assertEqual(formal["claim_status"], "not_established")
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["claim_status"], "not_established")


if __name__ == "__main__":
    unittest.main()
