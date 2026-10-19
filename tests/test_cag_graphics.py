import os
import re
import unittest
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.wolfram_graphics import render_wolfram_graphics


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

    def test_filled_shapes_lines_polygons_and_points_render_together(self) -> None:
        response = self.client.post(
            "/api/cag/v1/WolframLanguageCompute",
            json={
                "code": (
                    "Graphics[{Red, Rectangle[], Blue, Line[{{0, 0}, {2, 1}}], "
                    "Green, Polygon[{{0, 0}, {1, 0}, {0.5, 1}}], Point[{1, 1}]}]"
                )
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        image_url = re.search(r"!\[Image\]\((https?://[^)]+)\)", body["result"])
        self.assertIsNotNone(image_url, body["result"])
        assert image_url is not None
        image_response = self.client.get(urlsplit(image_url.group(1)).path)
        self.assertEqual(image_response.status_code, 200)
        self.assertIn("<rect ", image_response.text)
        self.assertIn("<polyline ", image_response.text)
        self.assertIn("<polygon ", image_response.text)
        self.assertIn('<circle cx=', image_response.text)

    def test_elliptic_disk_style_and_plot_range_options(self) -> None:
        svg = render_wolfram_graphics(
            "Graphics[{Opacity[0.5], Orange, Disk[{2, 3}, {1, 2}]}, "
            "PlotRange -> {{0, 4}, {0, 6}}, ImageSize -> 400]"
        )

        self.assertIsNotNone(svg)
        assert svg is not None
        self.assertIn('width="400" height="400"', svg)
        self.assertIn("<ellipse ", svg)
        self.assertIn('fill="#ed7d31"', svg)
        self.assertIn('fill-opacity="0.5"', svg)

    def test_arrow_path_renders_a_directed_endpoint_head(self) -> None:
        svg = render_wolfram_graphics(
            "Graphics[{Blue, Arrow[{{0, 0}, {1, 1}, {2, 1}}]}]"
        )

        self.assertIsNotNone(svg)
        assert svg is not None
        self.assertIn('<polyline points="', svg)
        self.assertIn('<polygon points="250,100 ', svg)
        self.assertIn('fill="#1565c0"', svg)

    def test_unknown_primitives_and_executable_syntax_remain_rejected(self) -> None:
        self.assertIsNone(
            render_wolfram_graphics("Graphics[{Red, BezierCurve[{{0,0},{1,1},{2,0}}]}]")
        )
        self.assertIsNone(render_wolfram_graphics("Graphics[{Red, Arrow[{{0,0},{0,0}}]}]"))
        self.assertIsNone(
            render_wolfram_graphics('Graphics[{Red, Circle[]}]; Export["x.svg", 1]')
        )


if __name__ == "__main__":
    unittest.main()
