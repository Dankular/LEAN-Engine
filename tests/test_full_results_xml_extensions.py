import json
import unittest
import xml.etree.ElementTree as ET

from leanproof_service.full_results import render_response


class FullResultsUnknownXmlFieldTests(unittest.TestCase):
    def test_additive_json_fields_survive_json_and_xml_serialization(self) -> None:
        payload = {
            "queryresult": {
                "success": True,
                "futuremetadata": {
                    "version": 2,
                    "flags": ["one", True, None],
                },
                "future:extension": {"source": "provider", "confidence": 0.8},
                "pods": [
                    {
                        "id": "Result",
                        "title": "Result",
                        "futurepodfield": {"rank": 3},
                        "subpods": [
                            {
                                "title": "",
                                "plaintext": "42",
                                "futureunits": {"symbol": "widgets"},
                            }
                        ],
                    }
                ],
                "assumptions": [
                    {
                        "type": "FutureType",
                        "values": [
                            {"input": "future:one", "metadata": {"rank": 1}}
                        ],
                        "futureassumptionfield": ["one", "two"],
                    }
                ],
            }
        }

        json_response = render_response(payload, "json")
        self.assertEqual(json.loads(json_response.body), payload)

        xml_response = render_response(payload, "xml")
        root = ET.fromstring(xml_response.body)
        self.assertEqual(root.attrib["success"], "true")

        metadata = root.find("futuremetadata")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.attrib["encoding"], "json")
        self.assertEqual(
            json.loads(metadata.text or ""),
            payload["queryresult"]["futuremetadata"],
        )

        renamed = root.find("field")
        self.assertIsNotNone(renamed)
        assert renamed is not None
        self.assertEqual(renamed.attrib["name"], "future:extension")
        self.assertEqual(
            json.loads(renamed.text or ""),
            payload["queryresult"]["future:extension"],
        )

        pod = root.find("pod")
        self.assertIsNotNone(pod)
        assert pod is not None
        pod_extension = pod.find("futurepodfield")
        self.assertIsNotNone(pod_extension)
        assert pod_extension is not None
        self.assertEqual(json.loads(pod_extension.text or ""), {"rank": 3})

        subpod_extension = pod.find("./subpod/futureunits")
        self.assertIsNotNone(subpod_extension)
        assert subpod_extension is not None
        self.assertEqual(
            json.loads(subpod_extension.text or ""),
            {"symbol": "widgets"},
        )

        assumption_extension = root.find("./assumptions/assumption/futureassumptionfield")
        self.assertIsNotNone(assumption_extension)
        assert assumption_extension is not None
        self.assertEqual(json.loads(assumption_extension.text or ""), ["one", "two"])

        value_extension = root.find("./assumptions/assumption/value/metadata")
        self.assertIsNotNone(value_extension)
        assert value_extension is not None
        self.assertEqual(json.loads(value_extension.text or ""), {"rank": 1})


if __name__ == "__main__":
    unittest.main()
