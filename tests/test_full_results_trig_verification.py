import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.full_results import _query_exact_trigonometric_code


class FullResultsTrigonometricVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["API_BEARER_TOKEN"] = "contract-test-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer contract-test-token"}

    def test_transpiles_rational_pi_angles_and_rejects_general_expressions(self) -> None:
        cases = {
            "sin(pi/12)": "Sin[Pi/12]",
            "cos(3*pi/8)": "Cos[3*Pi/8]",
            "tan(-pi/4)": "Tan[-Pi/4]",
            "sin(30*pi/180)": "Sin[Pi/6]",
            "sin(pi/3+pi/6)": "Sin[Pi/2]",
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(
                    _query_exact_trigonometric_code(expression),
                    expected,
                )
        self.assertIsNone(_query_exact_trigonometric_code("sin(x)"))
        self.assertIsNone(_query_exact_trigonometric_code("sin(pi/12)+cos(pi/6)"))

    def test_query_result_uses_the_lean_checked_exact_value(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "sin(pi/12)", "verify": "true", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(
            result_pod["subpods"][0]["plaintext"],
            "sqrt((1 - sqrt(3)/2)/2)",
        )


if __name__ == "__main__":
    unittest.main()
