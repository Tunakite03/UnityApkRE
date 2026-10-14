import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from uare.game_model import FileRecord, GameModel, Json, ModelError, ObjectRecord, identity, load_model, save_model


class ModelStreamingTests(unittest.TestCase):
    def test_invalid_record_does_not_replace_existing_evidence(self) -> None:
        obj = ObjectRecord(identity("Data/table", "table", 1), "Data/table", "table", 1,
                           "TextAsset", "Invalid", {"value": float("nan")}, "complete", (), None)
        model = GameModel((FileRecord("Data/table", "table", ()),), (obj,), (), ())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.json"
            path.write_text("previous evidence\n", encoding="utf-8")
            with self.assertRaises(ModelError):
                save_model(path, model)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous evidence\n")

    def test_large_model_is_saved_without_copying_all_object_payloads(self) -> None:
        samples: list[Json] = list(range(2000))
        objects = tuple(
            ObjectRecord(identity("Data/table", "table", index), "Data/table", "table", index,
                         "TextAsset", "Samples", {"samples": samples}, "complete", (), None)
            for index in range(120)
        )
        model = GameModel((FileRecord("Data/table", "table", ()),), objects, (), ())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.json"
            tracemalloc.start()
            try:
                save_model(path, model)
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak_bytes, 12 * 1024 * 1024,
                            "Saving duplicated the complete model instead of serializing one record at a time")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(raw["objects"]), 120)
            self.assertEqual(raw["objects"][-1]["data"]["samples"][-1], 1999)
            recovered = load_model(path)
            self.assertEqual(len(recovered.objects), 120)
            self.assertTrue(all(obj.data["samples"] == samples for obj in recovered.objects))
            self.assertIs(objects[0].data["samples"], samples)


if __name__ == "__main__":
    unittest.main()
