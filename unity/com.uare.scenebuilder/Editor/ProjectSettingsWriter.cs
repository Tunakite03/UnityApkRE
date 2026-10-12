using System.Collections.Generic;
using UnityEditor;

namespace Uare.SceneBuilder.Editor
{
    // Adds the original build's tags, layers and sorting layers to TagManager. Existing entries are kept; an entry that
    // conflicts with the original (same slot or name, different value) stops the build instead of being overwritten.
    // Sorting layers keep their original unique IDs, because renderers reference sorting layers by ID.
    internal static class ProjectSettingsWriter
    {
        private const string TagManagerPath = "ProjectSettings/TagManager.asset";

        public static void Apply(SceneSpec spec, SceneBuildReport report)
        {
            var assets = AssetDatabase.LoadAllAssetsAtPath(TagManagerPath);
            if (assets.Length == 0 || assets[0] == null)
            {
                throw new SceneBuildException($"Cannot load {TagManagerPath}");
            }

            using var manager = new SerializedObject(assets[0]);
            AddTags(manager.FindProperty("tags"), spec.tags, report);
            SetLayers(manager.FindProperty("layers"), spec.layers, report);
            SetSortingLayers(manager.FindProperty("m_SortingLayers"), spec.sortingLayers, report);
            if (manager.ApplyModifiedPropertiesWithoutUndo())
            {
                AssetDatabase.SaveAssets();
            }
        }

        private static void AddTags(SerializedProperty tags, string[] wanted, SceneBuildReport report)
        {
            var existing = new HashSet<string>(UnityEditorInternal.InternalEditorUtility.tags);
            for (var index = 0; index < tags.arraySize; index++)
            {
                existing.Add(tags.GetArrayElementAtIndex(index).stringValue);
            }

            foreach (var tag in wanted)
            {
                if (string.IsNullOrEmpty(tag) || existing.Contains(tag))
                {
                    continue;
                }

                tags.arraySize++;
                tags.GetArrayElementAtIndex(tags.arraySize - 1).stringValue = tag;
                existing.Add(tag);
                report.settingsChanges.Add("tag added: " + tag);
            }
        }

        private static void SetLayers(SerializedProperty layers, string[] wanted, SceneBuildReport report)
        {
            if (layers.arraySize < wanted.Length)
            {
                throw new SceneBuildException($"TagManager has {layers.arraySize} layer slots; the original build uses {wanted.Length}");
            }

            for (var index = 0; index < wanted.Length; index++)
            {
                var name = wanted[index];
                var slot = layers.GetArrayElementAtIndex(index);
                if (string.IsNullOrEmpty(name) || slot.stringValue == name)
                {
                    continue;
                }

                if (!string.IsNullOrEmpty(slot.stringValue))
                {
                    throw new SceneBuildException($"Layer {index} is '{slot.stringValue}' in this project but '{name}' in the original build");
                }

                for (var other = 0; other < layers.arraySize; other++)
                {
                    if (layers.GetArrayElementAtIndex(other).stringValue == name)
                    {
                        throw new SceneBuildException($"Layer name '{name}' is slot {other} in this project but slot {index} in the original build");
                    }
                }

                slot.stringValue = name;
                report.settingsChanges.Add($"layer {index} set: {name}");
            }
        }

        private static void SetSortingLayers(SerializedProperty layers, SortingLayerSpec[] wanted, SceneBuildReport report)
        {
            var current = new List<(string Name, uint Id)>();
            var locked = new HashSet<uint>();
            for (var index = 0; index < layers.arraySize; index++)
            {
                var layer = layers.GetArrayElementAtIndex(index);
                var id = (uint)layer.FindPropertyRelative("uniqueID").longValue;
                current.Add((layer.FindPropertyRelative("name").stringValue, id));
                if (layer.FindPropertyRelative("locked").boolValue)
                {
                    locked.Add(id);
                }
            }

            var desired = new List<(string Name, uint Id)>();
            foreach (var layer in wanted)
            {
                desired.Add((layer.name, (uint)layer.uniqueId));
            }

            if (Same(current, desired))
            {
                return;
            }

            // Every layer already in the project must exist in the original with the same ID; then the original order wins.
            foreach (var layer in current)
            {
                if (!desired.Contains(layer))
                {
                    throw new SceneBuildException(
                        $"Sorting layer '{layer.Name}' (id {layer.Id}) in this project does not match the original build's sorting layers");
                }
            }

            layers.arraySize = desired.Count;
            for (var index = 0; index < desired.Count; index++)
            {
                var layer = layers.GetArrayElementAtIndex(index);
                layer.FindPropertyRelative("name").stringValue = desired[index].Name;
                layer.FindPropertyRelative("uniqueID").longValue = desired[index].Id;
                layer.FindPropertyRelative("locked").boolValue = locked.Contains(desired[index].Id);
            }

            report.settingsChanges.Add("sorting layers set: " + string.Join(", ", desired.ConvertAll(l => $"{l.Name}={l.Id}")));
        }

        private static bool Same(List<(string Name, uint Id)> left, List<(string Name, uint Id)> right)
        {
            if (left.Count != right.Count)
            {
                return false;
            }

            for (var index = 0; index < left.Count; index++)
            {
                if (left[index] != right[index])
                {
                    return false;
                }
            }

            return true;
        }
    }
}
