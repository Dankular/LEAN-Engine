import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.computation import prepare_computation


class CertifiedNumericApproximationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "certified-numeric-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer certified-numeric-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_rational_approximations_include_a_bounded_error_claim(self) -> None:
        cases = {
            "N[1/3, 3]": ("0.333", 3, "1/2000"),
            "N[1/8, 2]": ("0.12", 2, "1/200"),
            "N[1.25, 2]": ("1.2", 2, "1/20"),
            "N[9.999, 3]": ("10.0", 3, "1/200"),
            "N[0.0009999, 3]": ("0.00100", 3, "1/2000000"),
            "N[-1/3, 3]": ("-0.333", 3, "1/2000"),
            "N[12345, 3]": ("12300", 3, "50"),
            "N[0, 500]": ("0", 500, "0"),
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                computation = prepare_computation(code)
                self.assertIsNotNone(computation)
                assert computation is not None
                self.assertEqual(
                    (computation.display_result, computation.numeric_precision,
                     computation.numeric_error_bound),
                    expected,
                )
                self.assertIn("|(", computation.lean_source)
                self.assertIn("#print axioms wolfram_language_compute", computation.lean_source)

    def test_endpoint_returns_decimal_and_lean_checked_error_bound(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={"code": "N[1/3, 3]"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["result"], "Out[1]= 0.333")
        self.assertEqual(body["evidence"]["kind"], "lean_kernel_checked")
        self.assertEqual(body["evidence"]["axiom_audit"]["status"], "passed")
        self.assertEqual(
            body["evidence"]["numeric_approximation"],
            {
                "value": "0.333",
                "precision_significant_digits": 3,
                "absolute_error_bound": "1/2000",
                "claim": "the exact rational input differs from value by at most this bound",
            },
        )

    def test_maximum_precision_remains_provable_within_the_route_budget(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={"code": "N[1/3, 500]", "timeConstraint": 60},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        evidence = response.json()["evidence"]
        self.assertEqual(evidence["numeric_approximation"]["precision_significant_digits"], 500)
        self.assertEqual(evidence["axiom_audit"]["status"], "passed")

    def test_halfway_rounding_is_inside_the_lean_checked_bound(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={"code": "N[1.25, 2]"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["evidence"]["numeric_approximation"]["value"], "1.2")
        self.assertEqual(body["evidence"]["numeric_approximation"]["absolute_error_bound"], "1/20")
        self.assertEqual(body["evidence"]["axiom_audit"]["status"], "passed")

    def test_irrational_and_invalid_precision_inputs_are_not_claimed(self) -> None:
        for code in (
            "N[Pi]",
            "N[Sqrt[2], 20]",
            "N[1/3, 0]",
            "N[1/3, 501]",
            "N[1/3, 2.5]",
            "N[1/3, 2, 3]",
        ):
            with self.subTest(code=code):
                self.assertIsNone(prepare_computation(code))


if __name__ == "__main__":
    unittest.main()
