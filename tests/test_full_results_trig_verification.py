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

    def test_query_proves_arithmetic_over_multiple_exact_trig_values(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "sin(pi/6)+cos(pi/3)",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(
            verification["claim_type"],
            "exact_trigonometric_expression_equality",
        )
        self.assertIn("exact_trig_0_wolfram_language_compute", verification["lean_source"])
        self.assertIn("exact_trig_1_wolfram_language_compute", verification["lean_source"])
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result_pod["subpods"][0]["plaintext"], "1")

    def test_query_proves_a_compound_nested_radical_value(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "sin(pi/12)+cos(pi/12)",
                "verify": "true",
                "output": "json",
            },
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
            "(sqrt((1 - sqrt(3)/2)/2) + sqrt((1 + sqrt(3)/2)/2))",
        )

    def test_cag_route_checks_the_compound_expression(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={
                "code": "Simplify[((Sin[Pi/6]+Cos[Pi/3])*2)^2/4]",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["result"], "Out[1]=1")
        self.assertEqual(payload["evidence"]["lean_status"], "checked")


if __name__ == "__main__":
    unittest.main()
