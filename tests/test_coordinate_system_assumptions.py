import os
import unittest

from fastapi.testclient import TestClient

from leanproof_service.api import app


class CoordinateSystemAssumptionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["API_BEARER_TOKEN"] = "contract-test-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer contract-test-token"}

    def test_documented_divergence_uses_cartesian_service_default(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "div(x rho,y z,z x)", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        assumption = next(
            item for item in queryresult["assumptions"]
            if item["type"] == "CoordinateSystem"
        )
        self.assertEqual(assumption["selected"], "coordinate:cartesian-3d")
        self.assertEqual(
            queryresult["interpretation"]["typed_ast"]["coordinate_system"],
            "Cartesian3D",
        )
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "rho + x + z")

    def test_cylindrical_override_changes_the_typed_result(self) -> None:
        response = self.client.get(
            "/v2/query",
            params=[
                ("input", "div(x rho,y z,z x)"),
                ("output", "json"),
                ("assumption", "coordinate:cylindrical-3d"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        assumption = next(
            item for item in queryresult["assumptions"]
            if item["type"] == "CoordinateSystem"
        )
        self.assertEqual(assumption["selection_source"], "caller_override")
        self.assertEqual(
            queryresult["interpretation"]["typed_ast"]["coordinate_system"],
            "Cylindrical3D",
        )
        self.assertEqual(queryresult["evidence"]["domain_conditions"], ["rho > 0"])
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "3*x")

    def test_per_occurrence_override_is_accepted_for_the_single_call(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params=[
                ("input", "div(x rho,y z,z x)"),
                ("assumption", "coordinate:cylindrical-3d@1"),
                ("output", "json"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        validate_result = response.json()["validatequeryresult"]
        assumption = next(
            item for item in validate_result["assumptions"]
            if item["type"] == "CoordinateSystem"
        )
        self.assertEqual(assumption["selection_source"], "per_occurrence_override")
        self.assertEqual(assumption["occurrences"][0]["id"], "CoordinateSystem:1")

    def test_cylindrical_divergence_is_lean_checked_under_chart_domain(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "div(x rho,y z,z x)",
                "assumption": "coordinate:cylindrical-3d",
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
        self.assertEqual(verification["claim_type"], "coordinate_divergence")
        self.assertEqual(verification["domain_conditions"], ["rho > 0"])
        self.assertEqual(verification["real_parameters"], ["x", "y"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_cartesian_divergence_can_be_lean_checked(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "div(x rho,y z,z x)",
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
        self.assertEqual(verification["claim_type"], "coordinate_divergence")
        self.assertEqual(verification["real_parameters"], ["rho"])
        self.assertNotIn("domain_conditions", verification)

    def test_spherical_divergence_uses_explicit_chart_and_is_lean_checked(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "div(0,1,0)",
                "assumption": "coordinate:spherical-3d",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        self.assertEqual(
            queryresult["interpretation"]["typed_ast"]["coordinate_variables"],
            ["r", "theta", "phi"],
        )
        self.assertEqual(
            queryresult["evidence"]["domain_conditions"],
            ["r > 0", "sin(theta) != 0"],
        )
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "cos(theta)/(r*sin(theta))")
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["claim_type"], "coordinate_divergence")
        self.assertEqual(verification["domain_conditions"], ["r > 0", "sin(theta) != 0"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_spherical_divergence_checks_radial_and_azimuthal_derivatives(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "div(r,0,phi)",
                "assumption": "coordinate:spherical-3d",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(
            result["subpods"][0]["plaintext"],
            "(3*r*sin(theta) + 1)/(r*sin(theta))",
        )
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_cartesian_gradient_is_lean_checked_by_default(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "grad(x^2 + y*z)", "verify": "true", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        self.assertEqual(
            queryresult["interpretation"]["typed_ast"]["coordinate_system"],
            "Cartesian3D",
        )
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "(2*x, z, y)")
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertEqual(verification["claim_type"], "coordinate_gradient")
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_cylindrical_gradient_checks_chart_condition(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "grad(rho)",
                "assumption": "coordinate:cylindrical-3d",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        self.assertEqual(queryresult["evidence"]["domain_conditions"], ["rho > 0"])
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(result["subpods"][0]["plaintext"], "(1, 0, 0)")
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertEqual(verification["domain_conditions"], ["rho > 0"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_spherical_gradient_checks_azimuthal_derivative(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={
                "input": "grad(phi)",
                "assumption": "coordinate:spherical-3d",
                "verify": "true",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        self.assertEqual(
            queryresult["evidence"]["domain_conditions"],
            ["r > 0", "sin(theta) != 0"],
        )
        result = next(pod for pod in queryresult["pods"] if pod["id"] == "Result")
        self.assertEqual(
            result["subpods"][0]["plaintext"], "(0, 0, 1/(r*sin(theta)))"
        )
        verification = queryresult["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_validatequery_exposes_the_gradient_chart_plan(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "grad(phi)",
                "assumption": "coordinate:spherical-3d",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        validate_result = response.json()["validatequeryresult"]
        self.assertEqual(
            validate_result["interpretation"]["typed_ast"]["operation"], "gradient"
        )
        self.assertEqual(
            validate_result["interpretation"]["typed_ast"]["coordinate_system"],
            "Spherical3D",
        )
        assumption = next(
            item for item in validate_result["assumptions"]
            if item["type"] == "CoordinateSystem"
        )
        self.assertEqual(assumption["selected"], "coordinate:spherical-3d")

    def test_curl_is_lean_checked_in_all_three_coordinate_charts(self) -> None:
        cases = [
            {
                "input": "curl(0,0,y)",
                "expected": "(1, 0, 0)",
                "domain": [],
            },
            {
                "input": "curl(0,0,theta)",
                "assumption": "coordinate:cylindrical-3d",
                "expected": "(1/rho, 0, 0)",
                "domain": ["rho > 0"],
            },
            {
                "input": "curl(0,0,phi)",
                "assumption": "coordinate:spherical-3d",
                "expected": "(phi*cos(theta)/(r*sin(theta)), -phi/r, 0)",
                "domain": ["r > 0", "sin(theta) != 0"],
            },
        ]
        for case in cases:
            params = {
                "input": case["input"],
                "verify": "true",
                "output": "json",
            }
            if "assumption" in case:
                params["assumption"] = case["assumption"]
            with self.subTest(input=case["input"]):
                response = self.client.get(
                    "/v2/query", params=params, headers=self.headers
                )

                self.assertEqual(response.status_code, 200, response.text)
                queryresult = response.json()["queryresult"]
                result = next(
                    pod for pod in queryresult["pods"] if pod["id"] == "Result"
                )
                self.assertEqual(result["subpods"][0]["plaintext"], case["expected"])
                self.assertEqual(queryresult["evidence"]["domain_conditions"], case["domain"])
                verification = queryresult["evidence"]["formal_verification"]
                self.assertEqual(verification["status"], "checked", verification)
                self.assertEqual(verification["claim_type"], "coordinate_curl")
                self.assertTrue(verification["verified"])
                self.assertEqual(verification["result_match"], True)
                self.assertEqual(verification["axiom_audit"]["status"], "passed")

    def test_curl_supports_bounded_sine_cosine_fields_in_all_charts(self) -> None:
        cases = [
            {
                "input": "curl(sin(y),0,0)",
                "expected": "(0, 0, -cos(y))",
                "domain": [],
            },
            {
                "input": "curl(0,sin(theta),0)",
                "assumption": "coordinate:cylindrical-3d",
                "expected": "(0, 0, sin(theta)/rho)",
                "domain": ["rho > 0"],
            },
            {
                "input": "curl(0,0,phi*sin(theta))",
                "assumption": "coordinate:spherical-3d",
                "expected": "(2*phi*cos(theta)/r, -phi*sin(theta)/r, 0)",
                "domain": ["r > 0", "sin(theta) != 0"],
            },
        ]
        for case in cases:
            params = {
                "input": case["input"],
                "verify": "true",
                "output": "json",
            }
            if "assumption" in case:
                params["assumption"] = case["assumption"]
            with self.subTest(input=case["input"]):
                response = self.client.get(
                    "/v2/query", params=params, headers=self.headers
                )

                self.assertEqual(response.status_code, 200, response.text)
                queryresult = response.json()["queryresult"]
                result = next(
                    pod for pod in queryresult["pods"] if pod["id"] == "Result"
                )
                self.assertEqual(result["subpods"][0]["plaintext"], case["expected"])
                self.assertEqual(queryresult["evidence"]["domain_conditions"], case["domain"])
                verification = queryresult["evidence"]["formal_verification"]
                self.assertEqual(verification["status"], "checked", verification)
                self.assertEqual(verification["claim_type"], "coordinate_curl")
                self.assertTrue(verification["verified"])
                self.assertEqual(verification["result_match"], True)
                self.assertEqual(verification["axiom_audit"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
