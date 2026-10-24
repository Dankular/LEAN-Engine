import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.matrix_algebra import MatrixAlgebraError, evaluate_matrix_algebra


class MatrixEvaluatorTests(unittest.TestCase):
    def result(self, text: str) -> str:
        outcome = evaluate_matrix_algebra(text)
        self.assertIsNotNone(outcome, text)
        assert outcome is not None
        return outcome.result_text

    def test_supported_forms(self) -> None:
        cases = {
            "determinant of {{1,2},{3,4}}": "-2",
            "Det[{{2,0},{0,3}}]": "6",
            "inverse of {{1,2},{3,4}}": "{{-2, 1}, {3/2, -1/2}}",
            "transpose of {{1,2,3},{4,5,6}}": "{{1, 4}, {2, 5}, {3, 6}}",
            "rank of {{1,2},{2,4}}": "1",
            "trace of [[1,2],[3,4]]": "5",
            "{{1,2},{3,4}} * {{0,1},{1,0}}": "{{2, 1}, {4, 3}}",
            "{{1,2},{3,4}} + {{1,1},{1,1}}": "{{2, 3}, {4, 5}}",
            "{{1,2},{3,4}}^3": "{{37, 54}, {81, 118}}",
            "2 {{1,2},{3,4}}": "{{2, 4}, {6, 8}}",
            "eigenvalues of {{2,0},{0,3}}": "{3, 2}",
            "eigenvalues of {{0,1},{-1,0}}": "{-i, i}",
            "characteristic polynomial of {{1,2},{3,4}}": "x^2 - 5*x - 2",
            "rref {{1,2},{3,4}}": "{{1, 0}, {0, 1}}",
            "determinant of {{1/2,0.5},{1,3}}": "1",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.result(text), expected)

    def test_non_matrix_input_does_not_match(self) -> None:
        for text in ("determinant", "factor x^2-1", "France population"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_matrix_algebra(text))

    def test_undefined_and_oversized_requests_are_rejected(self) -> None:
        big = "{" + ",".join("{" + ",".join("1" for _ in range(7)) + "}" for _ in range(7)) + "}"
        for text in (
            "inverse of {{1,2},{2,4}}",
            "determinant of {{1,2,3},{4,5,6}}",
            "{{1,2},{3,4}} + {{1,2,3}}",
            "{{1,2},{3,4}} * {{1,2,3}}",
            "determinant of {{1,x},{3,4}}",
            "determinant of {{1,2},{3}}",
            f"determinant of {big}",
            "{{1,2},{3,4}}^100",
            "eigenvalues of {{1,0,0,0,0},{0,1,0,0,0},{0,0,1,0,0},{0,0,0,1,0},{0,0,0,0,1}}",
        ):
            with self.subTest(text=text):
                with self.assertRaises(MatrixAlgebraError):
                    evaluate_matrix_algebra(text)


class MatrixQueryTests(unittest.TestCase):
    def query(self, text: str, **params: str) -> dict:
        token = "matrix-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={"input": text, "output": "json", **params},
                    headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["queryresult"]

    def test_inverse_has_matrix_scanner_and_grid_pod(self) -> None:
        queryresult = self.query("inverse of {{1,2},{3,4}}")
        pods = {pod["id"]: pod for pod in queryresult["pods"]}
        self.assertEqual(pods["Result"]["scanner"], "Matrix")
        self.assertEqual(
            pods["Result"]["subpods"][0]["plaintext"], "{{-2, 1}, {3/2, -1/2}}"
        )
        self.assertIn("MatrixForm", pods)
        self.assertEqual(queryresult["evidence"]["kind"], "matrix_algebra_result")
        self.assertFalse(queryresult["evidence"]["verified"])

    def test_verify_is_unsupported_and_errors_fail(self) -> None:
        queryresult = self.query("determinant of {{1,2},{3,4}}", verify="true")
        self.assertEqual(queryresult["evidence"]["formal_verification"]["status"], "unsupported")
        self.assertFalse(self.query("inverse of {{1,2},{2,4}}")["success"])


if __name__ == "__main__":
    unittest.main()
