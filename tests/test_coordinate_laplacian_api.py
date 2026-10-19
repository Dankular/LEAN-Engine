import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app


class CoordinateLaplacianApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["API_BEARER_TOKEN"] = "coordinate-laplacian-test-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer coordinate-laplacian-test-token"}

    def test_laplacian_is_lean_checked_in_each_supported_chart(self) -> None:
        cases = [
            ("laplacian(x^2 + y^2 + z^2)", None, []),
            (
                "laplacian(rho^2 + theta^2 + z^2)",
                "coordinate:cylindrical-3d",
                ["rho > 0"],
            ),
            (
                "laplacian(r^2)",
                "coordinate:spherical-3d",
                ["r > 0", "sin(theta) != 0"],
            ),
        ]
        for input_text, coordinate, domain_conditions in cases:
            params = {"input": input_text, "verify": "true", "output": "json"}
            if coordinate:
                params["assumption"] = coordinate
            with self.subTest(input=input_text):
                response = self.client.get(
                    "/v2/query", params=params, headers=self.headers
                )
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["queryresult"]
                verification = result["evidence"]["formal_verification"]
                self.assertEqual(verification["status"], "checked", verification)
                self.assertTrue(verification["verified"])
                self.assertEqual(verification["claim_type"], "coordinate_laplacian")
                self.assertEqual(
                    verification.get("domain_conditions", []), domain_conditions
                )
                self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_spherical_theta_laplacian_checks_angular_terms(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "laplacian(theta^2)",
                "assumption": "coordinate:spherical-3d",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        verification = response.json()["queryresult"]["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertEqual(verification["claim_type"], "coordinate_laplacian")
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_natural_language_laplacian_is_lean_checked(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "Find the Laplacian of x squared plus y squared plus z squared",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        self.assertEqual(
            result["interpretation"]["natural_language_plan"]["rule_id"],
            "coordinate_laplacian_scalar",
        )
        verification = result["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertEqual(verification["claim_type"], "coordinate_laplacian")
        self.assertTrue(verification["verified"])

    def test_validatequery_exposes_the_selected_laplacian_chart(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "Find the Laplacian of r squared in spherical coordinates",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["validatequeryresult"]
        typed_ast = result["interpretation"]["typed_ast"]
        self.assertEqual(typed_ast["operation"], "laplacian")
        self.assertEqual(typed_ast["coordinate_system"], "Spherical3D")
        self.assertEqual(typed_ast["second_derivative_terms"], ["2", "0", "0"])


if __name__ == "__main__":
    unittest.main()
