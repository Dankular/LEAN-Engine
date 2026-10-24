import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.number_theory import NumberTheoryError, evaluate_number_theory


class NumberTheoryEvaluatorTests(unittest.TestCase):
    def result(self, text: str) -> str:
        outcome = evaluate_number_theory(text)
        self.assertIsNotNone(outcome, text)
        assert outcome is not None
        return outcome.result_text

    def test_supported_forms(self) -> None:
        cases = {
            "factor 360": "2^3 × 3^2 × 5",
            "prime factorization of 97": "97 (prime)",
            "factor -12": "-1 × 2^2 × 3",
            "is 91 prime?": "91 is not a prime number",
            "is 97 a prime number": "97 is a prime number",
            "gcd of 12 and 18": "6",
            "greatest common divisor of 12, 18, 30": "6",
            "lcm(4, 6, 10)": "60",
            "17 mod 5": "2",
            "-7 mod 3": "2",
            "what is 2^100 mod 7": "2",
            "12!": "479001600",
            "factorial(5)": "120",
            "10 choose 3": "120",
            "binomial(10,3)": "120",
            "euler phi of 36": "12",
            "next prime after 100": "101",
            "10th prime": "29",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.result(text), expected)

    def test_symbolic_and_unrelated_inputs_do_not_match(self) -> None:
        for text in ("factor x^2-1", "gcd of x and y", "derivative of sin(x)", "France population"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_number_theory(text))

    def test_undefined_and_oversized_requests_are_rejected(self) -> None:
        for text in (
            "factor 0",
            "5 mod 0",
            "factorial(501)",
            "factor 1000000000000000000",
            "5000 choose 2",
            "gcd of 4",
        ):
            with self.subTest(text=text):
                with self.assertRaises(NumberTheoryError):
                    evaluate_number_theory(text)


class NumberTheoryQueryTests(unittest.TestCase):
    def query(self, text: str, **params: str) -> dict:
        token = "number-theory-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={"input": text, "output": "json", **params},
                    headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["queryresult"]

    def test_factorization_returns_result_and_divisor_pods(self) -> None:
        queryresult = self.query("factor 360")
        self.assertTrue(queryresult["success"])
        pods = {pod["id"]: pod for pod in queryresult["pods"]}
        self.assertEqual(pods["Result"]["subpods"][0]["plaintext"], "2^3 × 3^2 × 5")
        self.assertIn("24 divisors", pods["Divisors"]["subpods"][0]["plaintext"])
        self.assertEqual(queryresult["evidence"]["kind"], "number_theory_result")
        self.assertFalse(queryresult["evidence"]["verified"])

    def test_composite_primality_includes_factorization(self) -> None:
        queryresult = self.query("is 91 prime")
        pods = {pod["id"]: pod for pod in queryresult["pods"]}
        self.assertEqual(
            pods["Result"]["subpods"][0]["plaintext"], "91 is not a prime number"
        )
        self.assertEqual(pods["PrimeFactorization"]["subpods"][0]["plaintext"], "7 × 13")

    def test_verify_reports_unsupported_without_claiming_proof(self) -> None:
        queryresult = self.query("gcd of 12 and 18", verify="true")
        formal = queryresult["evidence"]["formal_verification"]
        self.assertEqual(formal["status"], "unsupported")
        self.assertFalse(formal["verified"])

    def test_out_of_bounds_request_is_not_a_success(self) -> None:
        queryresult = self.query("factorial(501)")
        self.assertFalse(queryresult["success"])

    def test_pod_filters_apply(self) -> None:
        queryresult = self.query("factor 360", includepodid="Result")
        self.assertEqual([pod["id"] for pod in queryresult["pods"]], ["Result"])


if __name__ == "__main__":
    unittest.main()
