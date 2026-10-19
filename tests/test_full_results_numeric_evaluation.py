import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.full_results import InterpretationError, _parse_operation_call


class FullResultsNumericEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "numeric-evaluation-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer numeric-evaluation-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_wolfram_n_syntax_accepts_bounded_decimal_precision(self) -> None:
        default_precision = _parse_operation_call("N[Pi]")
        requested_precision = _parse_operation_call("N[Sin[Pi/6], 40]")
        maximum_precision = _parse_operation_call("N[Pi, 500]")

        self.assertIsNotNone(default_precision)
        self.assertEqual(default_precision.operation, "numeric")
        self.assertEqual(default_precision.numeric_precision, 16)
        self.assertIsNotNone(requested_precision)
        self.assertEqual(requested_precision.expression, "Sin[Pi/6]")
        self.assertEqual(requested_precision.numeric_precision, 40)
        self.assertEqual(maximum_precision.numeric_precision, 500)
        with self.assertRaises(InterpretationError):
            _parse_operation_call("N[Pi, 501]")

    def test_query_returns_numeric_result_without_claiming_lean_verification(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "N[Sin[Pi/6], 40]", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(
            result_pod["subpods"][0]["plaintext"],
            "0.5000000000000000000000000000000000000000",
        )
        self.assertEqual(queryresult["evidence"]["engine"], "sympy")
        self.assertIs(queryresult["evidence"]["verified"], False)
        self.assertNotIn("DecimalApproximation", [pod["id"] for pod in queryresult["pods"]])

    def test_verify_certifies_supported_numeric_approximation(self) -> None:
        cases = {
            "N[Pi, 30]": "3.14159265358979323846264338328",
            "N[Sin[Pi/6], 40]": "0.5000000000000000000000000000000000000000",
        }
        for expression, expected_value in cases.items():
            with self.subTest(expression=expression):
                response = self.client.get(
                    "/v2/query",
                    params={"input": expression, "verify": "true", "output": "json"},
                    headers=self.headers,
                )

                self.assertEqual(response.status_code, 200, response.text)
                queryresult = response.json()["queryresult"]
                evidence = queryresult["evidence"]
                formal = evidence["formal_verification"]
                result_pod = next(
                    pod for pod in queryresult["pods"] if pod["id"] == "Result"
                )
                approximation = formal["numeric_approximation"]
                self.assertEqual(formal["status"], "checked")
                self.assertIs(formal["verified"], True)
                self.assertEqual(formal["claim_type"], "numeric_approximation")
                self.assertTrue(formal["result_match"])
                self.assertEqual(formal["axiom_audit"]["status"], "passed")
                self.assertEqual(approximation["value"], expected_value)
                self.assertEqual(
                    result_pod["subpods"][0]["plaintext"],
                    approximation["value"],
                )
                if "Pi, 30" in expression:
                    self.assertIn("pi_lower_bound", formal["lean_source"])

    def test_verify_leaves_unsupported_numeric_approximations_unverified(self) -> None:
        for expression in ("N[Exp[1], 30]", "N[Pi, 101]"):
            with self.subTest(expression=expression):
                response = self.client.get(
                    "/v2/query",
                    params={"input": expression, "verify": "true", "output": "json"},
                    headers=self.headers,
                )

                self.assertEqual(response.status_code, 200, response.text)
                formal = response.json()["queryresult"]["evidence"]["formal_verification"]
                self.assertEqual(formal["status"], "unsupported")
                self.assertIs(formal["verified"], False)


if __name__ == "__main__":
    unittest.main()
