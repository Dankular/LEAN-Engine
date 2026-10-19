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


if __name__ == "__main__":
    unittest.main()
