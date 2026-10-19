import json
import os
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.full_results import _query_envelope, render_response


class FullResultsPodProvenanceTests(unittest.TestCase):
    def test_computation_pods_expose_engine_proof_and_input_provenance(self) -> None:
        payload = _query_envelope(
            success=True,
            error=False,
            pods=[
                {"id": "Input", "title": "Input", "subpods": []},
                {"id": "Result", "title": "Result", "subpods": []},
            ],
            elapsed=0.01,
            parse_timing=0.001,
            datatypes="Mathematical",
            warnings=[],
            assumptions=[],
            evidence={
                "kind": "symbolic_result_with_lean_verification",
                "engine": "sympy",
                "verified": True,
                "formal_verification": {"verified": True, "status": "checked"},
            },
        )

        queryresult = json.loads(render_response(payload, "json").body)["queryresult"]
        input_pod, result_pod = queryresult["pods"]
        self.assertEqual(input_pod["evidence_kind"], "input_interpretation")
        self.assertFalse(input_pod["verified"])
        self.assertEqual(input_pod["source"][0]["component"], "input_interpreter")
        self.assertEqual(
            result_pod["evidence_kind"],
            "symbolic_result_with_lean_verification",
        )
        self.assertTrue(result_pod["verified"])
        self.assertEqual(
            [source["component"] for source in result_pod["source"]],
            ["computation_engine", "proof_checker"],
        )

        xml_root = ET.fromstring(render_response(payload, "xml").body)
        xml_result = xml_root.find("./pod[@id='Result']")
        self.assertIsNotNone(xml_result)
        assert xml_result is not None
        self.assertEqual(xml_result.attrib["evidence_kind"], "symbolic_result_with_lean_verification")
        self.assertEqual(xml_result.attrib["verified"], "true")
        sources = xml_result.find("source")
        self.assertIsNotNone(sources)
        assert sources is not None
        self.assertEqual(sources.attrib["encoding"], "json")
        self.assertEqual(len(json.loads(sources.text or "[]")), 2)

    def test_data_pods_expose_provider_sources_as_unverified_evidence(self) -> None:
        wikidata_source = {
            "name": "Wikidata",
            "url": "https://www.wikidata.org/",
        }
        payload = _query_envelope(
            success=True,
            error=False,
            pods=[
                {"id": "Input", "title": "Input", "subpods": []},
                {"id": "Population", "title": "Population", "subpods": []},
            ],
            elapsed=0.01,
            parse_timing=0.001,
            datatypes="Data",
            warnings=[],
            assumptions=[],
            evidence={"kind": "retrieved_source_data", "verified": False},
            sources=[wikidata_source],
        )

        queryresult = render_response(payload, "json").body
        result = json.loads(queryresult)["queryresult"]
        input_pod, data_pod = result["pods"]
        self.assertEqual(input_pod["evidence_kind"], "input_interpretation")
        self.assertEqual(data_pod["evidence_kind"], "retrieved_source_data")
        self.assertFalse(data_pod["verified"])
        self.assertEqual(data_pod["source"], [wikidata_source])

    def test_query_route_includes_pod_provenance(self) -> None:
        token = "pod-provenance-contract-token"
        with patch.dict(os.environ, {"API_BEARER_TOKEN": token}):
            with TestClient(app) as client:
                response = client.get(
                    "/v2/query",
                    params={"input": "2+2", "output": "json"},
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        pods = response.json()["queryresult"]["pods"]
        self.assertTrue(pods)
        for pod in pods:
            self.assertIn("source", pod)
            self.assertIn("evidence_kind", pod)
            self.assertIn("verified", pod)
        result = next(pod for pod in pods if pod["id"] == "Result")
        self.assertEqual(result["source"][0]["name"], "sympy")
        self.assertEqual(result["evidence_kind"], "symbolic_result")
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
