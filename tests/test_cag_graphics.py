import os
import re
import unittest
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from leanproof_service.api import app


class WolframGraphicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "graphics-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer graphics-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_red_circle_returns_fetchable_svg_image_url(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={"code": "Graphics[{Red, Circle[]}]", "line": 50},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        match = re.search(r"!\[Image\]\((https?://[^)]+)\)", body["result"])
        self.assertIsNotNone(match, body["result"])
        assert match is not None
        self.assertTrue(body["result"].startswith("Out[50]=!"))
        self.assertFalse(body["evidence"]["verified"])
        self.assertEqual(body["evidence"]["media_type"], "image/svg+xml")

        image_response = self.client.get(urlsplit(match.group(1)).path)
        self.assertEqual(image_response.status_code, 200)
        self.assertTrue(image_response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn('<circle cx="150" cy="150" r="100"', image_response.text)
        self.assertIn('stroke="#d62828"', image_response.text)

    def test_unsupported_graphics_primitive_remains_rejected(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={"code": "Graphics[{Red, Disk[]}]"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 501)


if __name__ == "__main__":
    unittest.main()
