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

    def test_verify_marks_numeric_approximation_as_outside_lean_fragment(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "N[Pi, 30]", "verify": "true", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        formal = response.json()["queryresult"]["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "unsupported")
        self.assertIs(formal["verified"], False)


if __name__ == "__main__":
    unittest.main()
