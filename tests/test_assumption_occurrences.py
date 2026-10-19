import os
import unittest
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from leanproof_service.api import app


class AssumptionOccurrenceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["API_BEARER_TOKEN"] = "contract-test-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer contract-test-token"}

    def test_json_reports_override_and_default_for_each_angle_call(self) -> None:
        response = self.client.get(
            "/v2/query",
            params=[
                ("input", "sin(30)+cos(60)"),
                ("output", "json"),
                ("assumption", "angle:degrees@1"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        angle = next(
            item for item in queryresult["assumptions"] if item["type"] == "AngleUnit"
        )
        self.assertEqual(
            [item["selected"] for item in angle["occurrences"]],
            ["angle:degrees", "angle:radians"],
        )
        self.assertEqual(
            [item["selection_source"] for item in angle["occurrences"]],
            ["per_occurrence_override", "service_default"],
        )
        input_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Input")
        self.assertEqual(
            input_pod["subpods"][0]["plaintext"],
            "sin((30)*pi/180)+cos(60)",
        )

    def test_xml_serializes_multiple_log_occurrence_choices(self) -> None:
        response = self.client.get(
            "/v2/query",
            params=[
                ("input", "log(100)+log(10)"),
                ("output", "xml"),
                ("assumption", "log:base-10@1"),
                ("assumption", "log:base-e@2"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.text)
        occurrences = root.findall(".//assumptions/assumption/occurrence")
        self.assertEqual(
            [item.attrib["selected"] for item in occurrences],
            ["log:base-10", "log:base-e"],
        )
        self.assertEqual(
            [item.attrib["id"] for item in occurrences],
            ["LogBase:1", "LogBase:2"],
        )

    def test_unparenthesized_log_function_can_be_overridden_per_occurrence(self) -> None:
        response = self.client.get(
            "/v2/query",
            params=[
                ("input", "log 100+log 10"),
                ("output", "json"),
                ("assumption", "function:log-base-10@1"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["queryresult"]
        function = next(
            item for item in payload["assumptions"] if item["type"] == "Function"
        )
        self.assertEqual(
            [item["selected"] for item in function["occurrences"]],
            ["function:log-base-10", "function:log-natural"],
        )
        self.assertNotIn("selected", function)
        self.assertEqual(
            payload["interpretation"]["expression"], "log(100,10)+log(10,E)"
        )

    def test_function_overrides_coexist_with_parenthesized_log_and_angle_choices(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params=[
                ("input", "log(100)+log 10+sin(30)"),
                ("assumption", "log:base-10@1"),
                ("assumption", "function:log-base-10@1"),
                ("assumption", "angle:degrees@1"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["interpretation"]["expression"],
            "log(100, 10)+log(10,10)+sin((30)*pi/180)",
        )
        self.assertEqual(
            {item["type"] for item in payload["assumptions"]},
            {"AngleUnit", "LogBase", "Function"},
        )

    def test_invalid_occurrence_selections_are_rejected(self) -> None:
        invalid_selections = [
            ["angle:degrees@3"],
            ["angle:degrees", "angle:radians@1"],
            ["angle:degrees@1", "angle:radians@1"],
            ["angle:degrees@" + "9" * 1000],
        ]
        for selections in invalid_selections:
            with self.subTest(selections=selections):
                params = [("input", "sin(30)+cos(60)")]
                params.extend(("assumption", item) for item in selections)
                response = self.client.get(
                    "/v1/interpret", params=params, headers=self.headers
                )
                self.assertEqual(response.status_code, 422)

    def test_function_query_wide_and_per_occurrence_choices_cannot_mix(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params=[
                ("input", "log 10+log 100"),
                ("assumption", "function:log-base-10"),
                ("assumption", "function:log-natural@2"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)

    def test_query_wide_function_choice_still_applies_to_all_unparenthesized_logs(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params=[
                ("input", "log 10+log 100"),
                ("assumption", "function:log-base-10"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        function = next(
            item for item in payload["assumptions"] if item["type"] == "Function"
        )
        self.assertEqual(function["selected"], "function:log-base-10")
        self.assertEqual(
            [item["selected"] for item in function["occurrences"]],
            ["function:log-base-10", "function:log-base-10"],
        )
        self.assertEqual(function["values"][0]["input"], "function:log-base-10")

    def test_compound_unit_assumption_defaults_and_query_wide_override(self) -> None:
        default_response = self.client.get(
            "/v1/interpret",
            params={"input": "10 m + 5m"},
            headers=self.headers,
        )
        self.assertEqual(default_response.status_code, 200, default_response.text)
        default_payload = default_response.json()
        unit = next(item for item in default_payload["assumptions"] if item["type"] == "Unit")
        self.assertEqual(unit["selected"], "unit:m-meters")
        self.assertEqual(
            [item["selected"] for item in unit["occurrences"]],
            ["unit:m-meters", "unit:m-meters"],
        )
        self.assertEqual(
            default_payload["interpretation"]["typed_ast"]["unit_bindings"],
            {"unit_meter": "meter"},
        )

        override_response = self.client.get(
            "/v1/interpret",
            params=[("input", "10 m + 5m"), ("assumption", "unit:m-minutes")],
            headers=self.headers,
        )
        self.assertEqual(override_response.status_code, 200, override_response.text)
        override_payload = override_response.json()
        unit = next(item for item in override_payload["assumptions"] if item["type"] == "Unit")
        self.assertEqual(unit["selected"], "unit:m-minutes")
        self.assertEqual(override_payload["result"], "15*minute")

    def test_compound_unit_assumption_can_select_one_occurrence(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params=[
                ("input", "10 m * 5m"),
                ("assumption", "unit:m-minutes@2"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        unit = next(item for item in payload["assumptions"] if item["type"] == "Unit")
        self.assertEqual(
            [item["selected"] for item in unit["occurrences"]],
            ["unit:m-meters", "unit:m-minutes"],
        )
        self.assertEqual(
            [item["selection_source"] for item in unit["occurrences"]],
            ["service_default", "per_occurrence_override"],
        )
        self.assertEqual(
            payload["interpretation"]["typed_ast"]["unit_bindings"],
            {"unit_meter": "meter", "unit_minute": "minute"},
        )

    def test_full_results_route_exposes_compound_unit_occurrences(self) -> None:
        response = self.client.get(
            "/v2/query",
            params=[
                ("input", "10 m * 5 m"),
                ("output", "json"),
                ("assumption", "unit:m-minutes@2"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        unit = next(item for item in queryresult["assumptions"] if item["type"] == "Unit")
        self.assertEqual(
            [item["selected"] for item in unit["occurrences"]],
            ["unit:m-meters", "unit:m-minutes"],
        )
        self.assertTrue(any(pod["id"] == "Result" for pod in queryresult["pods"]))

    def test_compound_explicit_units_are_bound_and_dimension_checked(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params={"input": "10 m + 5 cm"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        unit = next(item for item in payload["assumptions"] if item["type"] == "Unit")
        self.assertEqual(len(unit["occurrences"]), 1)
        self.assertEqual(
            payload["interpretation"]["typed_ast"]["unit_bindings"],
            {"unit_meter": "meter", "unit_centimeter": "centimeter"},
        )

        full_results = self.client.get(
            "/v2/query",
            params=[
                ("input", "10 m + 5 cm"),
                ("output", "json"),
                ("format", "plaintext"),
                ("format", "minput"),
            ],
            headers=self.headers,
        )
        self.assertEqual(full_results.status_code, 200, full_results.text)
        queryresult = full_results.json()["queryresult"]
        input_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Input")
        self.assertEqual(
            input_pod["subpods"][0]["plaintext"],
            "10*meters + 5*centimeters",
        )
        self.assertEqual(
            input_pod["subpods"][0]["minput"],
            'Quantity[10, "Meters"] + Quantity[5, "Centimeters"]',
        )

    def test_compound_explicit_units_reject_incompatible_dimensions(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params={"input": "10 m + 5 sec"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)

    def test_incompatible_per_occurrence_units_are_rejected_in_addition(self) -> None:
        response = self.client.get(
            "/v1/interpret",
            params=[
                ("input", "10m + 5m"),
                ("assumption", "unit:m-minutes@2"),
            ],
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)

    def test_full_results_preserves_assumptions_when_dimensions_conflict(self) -> None:
        params = [
            ("input", "10 m + 5m"),
            ("output", "json"),
            ("assumption", "unit:m-minutes@2"),
        ]
        response = self.client.get("/v2/query", params=params, headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        queryresult = response.json()["queryresult"]
        self.assertFalse(queryresult["success"])
        unit = next(item for item in queryresult["assumptions"] if item["type"] == "Unit")
        self.assertEqual(
            [item["selected"] for item in unit["occurrences"]],
            ["unit:m-meters", "unit:m-minutes"],
        )
        self.assertIn("incompatible physical dimensions", queryresult["tips"][0]["text"])
        self.assertEqual(
            queryresult["interpretation"]["typed_ast"]["unit_bindings"],
            {"unit_meter": "meter", "unit_minute": "minute"},
        )
        self.assertIn("rejected by deterministic validation", queryresult["qualification"])

        validation = self.client.get(
            "/v2/validatequery",
            params=params,
            headers=self.headers,
        )
        self.assertEqual(validation.status_code, 200, validation.text)
        validate_result = validation.json()["validatequeryresult"]
        validate_unit = next(
            item for item in validate_result["assumptions"] if item["type"] == "Unit"
        )
        self.assertEqual(
            [item["selected"] for item in validate_unit["occurrences"]],
            ["unit:m-meters", "unit:m-minutes"],
        )


if __name__ == "__main__":
    unittest.main()
