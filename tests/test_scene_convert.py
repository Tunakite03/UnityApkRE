import json
import tempfile
import unittest
from pathlib import Path

from uare.scene_convert import convert_hierarchy, convert_scenes_and_prefabs, _YAML_HEADER


class ConvertHierarchyTests(unittest.TestCase):
    def test_single_gameobject(self):
        hierarchy = {
            "roots": [{
                "name": "Player",
                "components": [
                    {"class": "Transform", "data": {
                        "m_LocalPosition": {"x": 1, "y": 2, "z": 3},
                        "m_LocalRotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1}
                    }}
                ],
                "children": []
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.unity"
            go, comp = convert_hierarchy(hierarchy, out, is_scene=True)
            self.assertEqual(go, 1)
            content = out.read_text()
            self.assertIn("Player", content)
            self.assertIn("Transform:", content)
            self.assertIn(_YAML_HEADER.strip(), content)
            self.assertIn("!u!1", content)
            self.assertIn("!u!4", content)

    def test_nested_children(self):
        hierarchy = {
            "roots": [{
                "name": "Parent",
                "components": [{"class": "Transform", "data": None}],
                "children": [{
                    "name": "Child",
                    "components": [{"class": "Transform", "data": None}],
                    "children": []
                }]
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.prefab"
            go, comp = convert_hierarchy(hierarchy, out, is_scene=False)
            self.assertEqual(go, 2)
            content = out.read_text()
            self.assertIn("Parent", content)
            self.assertIn("Child", content)
            self.assertNotIn("OcclusionCullingSettings", content)

    def test_monobehaviour_component(self):
        hierarchy = {
            "roots": [{
                "name": "UIRoot",
                "components": [
                    {"class": "Transform", "data": None},
                    {"class": "MonoBehaviour", "script": "GameManager", "data": {"m_Score": 100}}
                ],
                "children": []
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.unity"
            go, comp = convert_hierarchy(hierarchy, out, is_scene=True)
            self.assertEqual(go, 1)
            self.assertEqual(comp, 1)
            content = out.read_text()
            self.assertIn("MonoBehaviour:", content)
            self.assertIn("GameManager", content)

    def test_scene_has_preamble(self):
        hierarchy = {"roots": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "empty.unity"
            convert_hierarchy(hierarchy, out, is_scene=True)
            content = out.read_text()
            self.assertIn("OcclusionCullingSettings:", content)
            self.assertIn("RenderSettings:", content)


class ConvertScenesAndPrefabsTests(unittest.TestCase):
    def test_converts_scene_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            scene_dir = source / "scenes" / "Level1"
            scene_dir.mkdir(parents=True)
            hierarchy = {
                "roots": [{"name": "Main Camera", "components": [
                    {"class": "Transform", "data": None},
                    {"class": "Camera", "data": None}
                ], "children": []}]
            }
            (scene_dir / "hierarchy.json").write_text(json.dumps(hierarchy))
            out = Path(tmpdir) / "out"
            result = convert_scenes_and_prefabs(source, out)
            self.assertEqual(result.scenes_converted, 1)
            self.assertTrue((out / "Scenes" / "Level1.unity").is_file())

    def test_writes_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()
            out = Path(tmpdir) / "out"
            result = convert_scenes_and_prefabs(source, out)
            self.assertEqual(result.scenes_converted, 0)
            report = json.loads((out / "convert_report.json").read_text())
            self.assertEqual(report["scenes"], 0)


if __name__ == "__main__":
    unittest.main()
