import os
import unittest
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from leanproof_service.api import app


class FullResultsCellFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "cell-format-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer cell-format-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_json_cell_format_is_returned_without_a_format_warning(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "2+2", "format": "cell", "output": "json"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        self.assertFalse(
            any("not available" in warning["text"] for warning in queryresult["warnings"])
        )
        input_pod, result_pod = queryresult["pods"][:2]
        self.assertIn('RowBox[{"2+2"}]', input_pod["subpods"][0]["cell"])
        self.assertIn('RowBox[{"4"}]', result_pod["subpods"][0]["cell"])
        self.assertNotIn("plaintext", result_pod["subpods"][0])

    def test_xml_cell_format_uses_cell_elements(self) -> None:
        response = self.client.get(
            "/v2/query",
            params={"input": "2+2", "format": "cell", "output": "xml"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.text)
        cells = root.findall(".//cell")
        self.assertEqual(len(cells), 2)
        self.assertIn('RowBox[{"4"}]', cells[1].text or "")


if __name__ == "__main__":
    unittest.main()
