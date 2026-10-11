import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from uare.blueprint import blueprint, build_report, scene_reports
from uare.game_model import (FileRecord, GameModel, Json, ModelError, ObjectRecord,
                            closure, identity, load_model, outgoing, resolve, save_model)
from uare.project_export import Binding, export_project
from uare.rebuild import rebuild
from uare.asset_import import guid_for, meta_content


def ptr(file_id: int, path_id: int) -> dict[str, Json]:
    return {"m_FileID": file_id, "m_PathID": path_id}


def obj(source: str, file: str, pid: int, cls: str, data: dict[str, Json]) -> ObjectRecord:
    return ObjectRecord(identity(source, file, pid), source, file, pid, cls,
                        str(data.get("m_Name", cls)), data, "complete", (), None)


def fixture_model() -> GameModel:
    scene = "Data/level0"
    bundle = "Data/shared.bundle"
    objects = (
        obj(scene, "level0", 1, "GameObject", {"m_Name": "Game UI", "m_IsActive": 0, "m_Layer": 5,
            "m_Component": [{"component": ptr(0, 2)}, {"component": ptr(0, 3)}]}),
        obj(scene, "level0", 2, "RectTransform", {"m_GameObject": ptr(0, 1), "m_Father": ptr(0, 0),
            "m_Children": [], "m_AnchorMin": {"x": 0.2, "y": 0.3}, "m_SizeDelta": {"x": 128, "y": 32}}),
        obj(scene, "level0", 3, "MonoBehaviour", {"m_GameObject": ptr(0, 1), "m_Script": ptr(1, 10),
            "health": 100, "icon": ptr(1, 20), "controller": ptr(1, 30), "missing": ptr(1, 999),
            "m_OnClick": {"m_PersistentCalls": {"m_Calls": [{"m_Target": ptr(0, 3), "m_MethodName": "StartGame"}]}}}),
        obj(bundle, "CAB-shared", 10, "MonoScript", {"m_ClassName": "GameUI", "m_Namespace": "Game",
            "m_AssemblyName": "Assembly-CSharp.dll"}),
        obj(bundle, "CAB-shared", 20, "Texture2D", {"m_Name": "Icon"}),
        obj(bundle, "CAB-shared", 30, "AnimatorController", {"m_Name": "UIAnimation", "m_AnimationClips": [ptr(0, 31)],
            "m_AnimatorParameters": [{"m_Name": "Open", "m_Type": 4}]}),
        obj(bundle, "CAB-shared", 31, "AnimationClip", {"m_Name": "Open", "m_Events": [{"functionName": "OnOpen"}]}),
    )
    return resolve((FileRecord(scene, "level0", ("archive:/CAB-shared/CAB-shared",)),
                    FileRecord(bundle, "CAB-shared", ())), objects, ())


class ModelTests(unittest.TestCase):
    def test_external_local_null_and_missing_references_have_distinct_status(self) -> None:
        model = fixture_model()
        edges = outgoing(model)
        refs = {r.field: r for r in edges[identity("Data/level0", "level0", 3)]}
        self.assertEqual(refs["m_Script"].target_id, identity("Data/shared.bundle", "CAB-shared", 10))
        self.assertEqual(refs["missing"].status, "missing_object")
        self.assertEqual(refs["m_GameObject"].target_id, identity("Data/level0", "level0", 1))
        self.assertEqual(next(r for r in model.references if r.field == "m_Father").status, "null")

    def test_duplicate_path_ids_in_two_serialized_files_do_not_collide(self) -> None:
        files = (FileRecord("bundle", "A", ("B",)), FileRecord("bundle", "B", ()))
        objects = (obj("bundle", "A", 1, "MonoBehaviour", {"other": ptr(1, 1)}),
                   obj("bundle", "B", 1, "Texture2D", {}))
        model = resolve(files, objects, ())
        self.assertNotEqual(objects[0].id, objects[1].id)
        self.assertEqual(model.references[0].target_id, objects[1].id)

    def test_ambiguous_external_is_not_guessed_even_when_only_one_has_path_id(self) -> None:
        files = (FileRecord("scene", "level0", ("CAB",)), FileRecord("a", "CAB", ()), FileRecord("b", "CAB", ()))
        objects = (obj("scene", "level0", 1, "MonoBehaviour", {"target": ptr(1, 2)}),
                   obj("a", "CAB", 2, "Texture2D", {}))
        model = resolve(files, objects, ())
        self.assertEqual(model.references[0].status, "ambiguous")
        self.assertIsNone(model.references[0].target_id)

    def test_cycles_terminate_without_mutating_input(self) -> None:
        model = fixture_model()
        original = copy.deepcopy(model)
        ids = closure({identity("Data/level0", "level0", 1)}, outgoing(model))
        self.assertEqual(len(ids), 7)
        self.assertEqual(model, original)

    def test_model_roundtrip_rejects_identity_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.json"
            model = fixture_model()
            save_model(path, model)
            self.assertEqual(load_model(path), model)
            raw = json.loads(path.read_text())
            raw["objects"][0]["id"] = "wrong"
            path.write_text(json.dumps(raw))
            with self.assertRaises(ModelError):
                load_model(path)

    def test_invalid_pptr_field_fails_loudly(self) -> None:
        with self.assertRaises(ModelError):
            resolve((FileRecord("a", "a", ()),), (obj("a", "a", 1, "MonoBehaviour", {
                "bad": {"m_FileID": True, "m_PathID": 3}}),), ())

    def test_binary_textasset_surrogates_roundtrip_through_blueprint(self) -> None:
        payload = b"\xc4\xff\x00".decode("utf-8", "surrogateescape")
        model = resolve((FileRecord("bundle", "CAB", ()),),
                        (obj("bundle", "CAB", 1, "TextAsset", {"m_Script": payload}),), ())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_model(root / "extracted-assets" / "game_model.json", model)
            blueprint(root, root / "blueprint", root / "extracted-assets")
            recovered = load_model(root / "blueprint" / "game_model.json")
            self.assertEqual(recovered.objects[0].data["m_Script"], payload)
            page = (root / "blueprint" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("\\udcc4", page)
            chunk = root / "blueprint" / "objects" / f"{model.objects[0].id[:2]}.js"
            script = chunk.read_text(encoding="utf-8")
            prefix = "window.uareReceiveObjects("
            self.assertTrue(script.startswith(prefix))
            data = json.loads(script[len(prefix):-3])
            self.assertEqual(data[model.objects[0].id]["m_Script"], payload)


class BlueprintTests(unittest.TestCase):
    def test_streamed_scene_hashes_resolve_null_container_entries_within_bundle(self) -> None:
        files = (FileRecord("scenes.bundle", "CAB-index", ()),
                 FileRecord("scenes.bundle", "CAB-main", ()),
                 FileRecord("other.bundle", "CAB-main", ()))
        objects = (
            obj("scenes.bundle", "CAB-index", 1, "AssetBundle", {
                "m_IsStreamedSceneAssetBundle": True,
                "m_SceneHashes": [["Assets/Scenes/Main.unity", "CAB-main"]],
                "m_Container": [["Assets/Scenes/Main.unity", {"asset": ptr(0, 0)}]]}),
            obj("scenes.bundle", "CAB-main", 1, "GameObject", {"m_Name": "Scene root"}),
            obj("other.bundle", "CAB-main", 1, "GameObject", {"m_Name": "Unclassified root"}),
        )
        reports = scene_reports(resolve(files, objects, ()))
        scenes = [r for r in reports if r["kind"] == "scene"]
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["name"], "Assets/Scenes/Main.unity")
        self.assertEqual(scenes[0]["classification_evidence"], "AssetBundle.m_SceneHashes")
        self.assertEqual(scenes[0]["source"], "scenes.bundle")
        self.assertEqual(sum(r["kind"] == "unclassified_hierarchy" for r in reports), 1)

    def test_scene_usage_scripts_animations_and_events_are_joined(self) -> None:
        scope = scene_reports(fixture_model())[0]
        self.assertEqual(scope["kind"], "scene")
        self.assertIn(identity("Data/shared.bundle", "CAB-shared", 20), scope["direct_assets"])
        self.assertIn(identity("Data/shared.bundle", "CAB-shared", 31), scope["transitive_assets"])
        self.assertEqual(len(scope["animation_objects"]), 2)
        self.assertEqual(scope["script_bindings"][0]["status"], "resolved")
        self.assertEqual({e["method"] for e in scope["events"]}, {"StartGame", "OnOpen"})
        self.assertEqual(scope["logic"]["status"], "unknown")
        self.assertEqual(len(scope["unresolved_references"]), 1)

    def test_arbitrary_bundle_hierarchy_is_not_called_a_prefab(self) -> None:
        model = fixture_model()
        objects = tuple(replace(o, id=identity("bundle", "CAB", o.path_id), source="bundle", serialized_file="CAB")
                        for o in model.objects if o.source == "Data/level0")
        converted = resolve((FileRecord("bundle", "CAB", ()),), objects, ())
        self.assertEqual(scene_reports(converted)[0]["kind"], "unclassified_hierarchy")

    def test_prefab_classification_requires_container_evidence(self) -> None:
        objects = (obj("bundle", "CAB", 1, "GameObject", {"m_Name": "Player", "m_Component": [{"component": ptr(0, 2)}]}),
                   obj("bundle", "CAB", 2, "Transform", {"m_GameObject": ptr(0, 1), "m_Father": ptr(0, 0), "m_Children": []}),
                   obj("bundle", "CAB", 3, "AssetBundle", {"m_Container": [["Assets/Player.prefab", {"asset": ptr(0, 1)}]]}))
        report = scene_reports(resolve((FileRecord("bundle", "CAB", ()),), objects, ()))[0]
        self.assertEqual(report["kind"], "prefab")
        self.assertEqual(report["classification_evidence"], "AssetBundle.m_Container")

    def test_safe_offline_html_and_version_unknown_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            model = fixture_model()
            extra = obj("Data/shared.bundle", "CAB-shared", 80, "MonoScript", {
                "m_ClassName": "</script><script>alert(1)</script>", "m_Namespace": "Firebase.Auth", "m_AssemblyName": "Firebase.Auth.dll"})
            model = resolve(model.files, model.objects + (extra,), ())
            save_model(work / "extracted-assets" / "game_model.json", model)
            result = blueprint(work, work / "blueprint", work / "extracted-assets")
            self.assertEqual(result.scenes, 1)
            report = json.loads((work / "blueprint" / "report.json").read_text())
            self.assertEqual(report["dependencies"][0]["name"], "Firebase")
            self.assertIsNone(report["dependencies"][0]["original_version"])
            page = (work / "blueprint" / "index.html").read_text()
            self.assertNotIn("</script><script>alert(1)</script>", page)
            self.assertNotIn("fetch(", page)

    def test_observations_validate_scope_and_preserve_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "observations.json").write_text(json.dumps({"observations": [{"scope_id": "missing",
                "action": "Tap Play", "result": "Gameplay appears", "evidence": "recording:frame100"}]}))
            with self.assertRaises(ModelError):
                build_report(fixture_model(), root)

    def test_behavior_citations_validate_bounds_and_hash_actual_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "code.cs").write_text("void Win() {\n    rewards.Grant();\n}\n")
            claims = {"claims": [{"scope_id": "game", "system": "rewards", "statement": "Win calls Grant",
                "basis": "code_analysis", "citations": [{"path": "code.cs", "start_line": 1, "end_line": 3}]}]}
            (root / "behavior.json").write_text(json.dumps(claims))
            report = build_report(fixture_model(), root)
            self.assertEqual(len(report["behavior_claims"][0]["citations"][0]["sha256"]), 64)
            claims["claims"][0]["citations"][0]["end_line"] = 100
            (root / "behavior.json").write_text(json.dumps(claims))
            with self.assertRaises(ModelError):
                build_report(fixture_model(), root)

    def test_citation_cannot_escape_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            (root.parent / "outside.cs").write_text("outside")
            (root / "behavior.json").write_text(json.dumps({"claims": [{"scope_id": "game", "system": "test",
                "statement": "test", "basis": "inference", "citations": [
                    {"path": "../outside.cs", "start_line": 1, "end_line": 1}]}]}))
            with self.assertRaises(ModelError):
                build_report(fixture_model(), root)

    def test_script_metadata_members_are_evidence_not_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "il2cpp").mkdir()
            (root / "il2cpp" / "types.tsv").write_text("42\tGame\tGameUI\n")
            (root / "il2cpp" / "members.tsv").write_text("42\tGameUI\tmethod\t15\tStartGame\n")
            report = build_report(fixture_model(), root)
            script = report["script_inventory"][0]
            self.assertEqual(script["metadata_match"], "unique_name_candidate")
            self.assertEqual(script["members"][0]["name"], "StartGame")
            self.assertEqual(script["behavior"], "not_analyzed")

    def test_empty_scenes_have_distinct_scope_ids(self) -> None:
        model = resolve((FileRecord("Data/level0", "level0", ()), FileRecord("Data/level1", "level1", ())), (), ())
        scopes = scene_reports(model)
        self.assertEqual(len({s["id"] for s in scopes}), 2)


class ProjectExportTests(unittest.TestCase):
    def test_convert_scenes_uses_canonical_model_when_available(self) -> None:
        from uare.scene_convert import convert_scenes_and_prefabs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_model(root / "source" / "game_model.json", fixture_model())
            result = convert_scenes_and_prefabs(root / "source", root / "converted")
            self.assertEqual(result.scenes_converted, 1)
            scene = next((root / "converted" / "Scenes").glob("*.unity")).read_text()
            self.assertIn("RectTransform:", scene)
            self.assertIn("health: 100", scene)
            report = json.loads((root / "converted" / "convert_report.json").read_text())
            self.assertEqual(report["source"], "game_model")
            self.assertGreater(report["errors"], 0)

    def test_preserves_rect_transform_custom_fields_and_remaps_local_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            result = export_project(fixture_model(), project, {})
            self.assertEqual(result.scenes, 1)
            scene = next((project / "Assets" / "Scenes").glob("*.unity")).read_text()
            self.assertIn("!u!224", scene)
            self.assertIn("RectTransform:", scene)
            self.assertIn("m_AnchorMin:", scene)
            self.assertIn("health: 100", scene)
            self.assertIn("m_IsActive: 0", scene)
            self.assertNotIn("m_PathID", scene)
            self.assertTrue(any(b.get("field") == "missing" for b in result.blockers))

    def test_real_script_and_media_bindings_match_generated_meta_guids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            script = project / "Assets" / "Scripts" / "GameUI.cs"
            script.parent.mkdir(parents=True)
            script.write_text("namespace Game { public class GameUI : UnityEngine.MonoBehaviour {} }")
            tid = identity("Data/shared.bundle", "CAB-shared", 20)
            media = {tid: Binding("Assets/Textures/Icon.png", 2800000, guid_for("Textures/Icon.png"), 3)}
            result = export_project(fixture_model(), project, media)
            sid = identity("Data/shared.bundle", "CAB-shared", 10)
            self.assertEqual(result.bindings[sid].guid, guid_for("Scripts/GameUI.cs"))
            scene = next((project / "Assets" / "Scenes").glob("*.unity")).read_text()
            self.assertIn(guid_for("Scripts/GameUI.cs"), scene)
            self.assertIn(guid_for("Textures/Icon.png"), scene)
            self.assertIn("PrefabImporter:", meta_content("Prefabs/Player.prefab", False))

    def test_rebuild_requires_model_and_does_not_guess_package_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            (work / "analysis").mkdir(parents=True)
            (work / "analysis" / "report.json").write_text(json.dumps({"scripting_backend": "il2cpp",
                "inventory": {"unity_versions": {"2022.3.10f1": 1}}}))
            with self.assertRaises(ModelError):
                rebuild(work, Path(tmp) / "legacy", work / "extracted-assets")
            save_model(work / "extracted-assets" / "game_model.json", fixture_model())
            out = Path(tmp) / "project"
            rebuild(work, out, work / "extracted-assets")
            self.assertEqual(json.loads((out / "Packages" / "manifest.json").read_text()), {"dependencies": {}})
            report = json.loads((out / "rebuild_report.json").read_text())
            self.assertEqual(report["status"], "structural_draft")
            self.assertGreater(report["reconstruction_blockers"], 0)
            self.assertEqual(report["validation"]["play_mode"], "not_tested")
            self.assertTrue((out / "Reconstruction" / "Blueprint" / "index.html").is_file())

    def test_invalid_package_selection_rolls_back_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            (work / "analysis").mkdir(parents=True)
            (work / "analysis" / "report.json").write_text(json.dumps({"inventory": {"unity_versions": {"2022.3.10f1": 1}}}))
            save_model(work / "extracted-assets" / "game_model.json", fixture_model())
            (work / "rebuild-packages.json").write_text(json.dumps({"dependencies": {"com.unity.addressables": "latest"}}))
            out = Path(tmp) / "project"
            with self.assertRaises(ModelError):
                rebuild(work, out, work / "extracted-assets")
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
