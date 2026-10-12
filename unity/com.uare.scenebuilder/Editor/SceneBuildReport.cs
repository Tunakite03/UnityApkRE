using System;
using System.Collections.Generic;
using UnityEngine;

namespace Uare.SceneBuilder.Editor
{
    [Serializable]
    internal sealed class NameCount
    {
        public string name;
        public int count;
    }

    // Outcome of writing spec rows into one population of serialized objects.
    [Serializable]
    internal sealed class PropertyCounts
    {
        public int applied;
        public int unmatched;
        public int typeMismatch;
        // Rows not written by design: placeholder fields kept as JSON, rows of objects that were not created.
        public int notWritten;
        public List<string> unmatchedSamples = new List<string>();
        public List<string> mismatchSamples = new List<string>();
    }

    // What one scene build did and what it could not do. Serialized with JsonUtility (schema uare.scene-build v2).
    [Serializable]
    internal sealed class SceneBuildReport
    {
        private const int SampleLimit = 60;

        public string schema = "uare.scene-build";
        public int version = 2;
        public string scene;
        public string scenePath;
        public string specPath;
        public string scopeId;
        public string originalPath;
        public string rootOrderBasis;

        public int gameObjects;
        public int components;
        public List<NameCount> componentClasses = new List<NameCount>();
        public List<NameCount> boundScripts = new List<NameCount>();
        // Bound to a type from an Il2CppDumper stub assembly: original fields and identity, no original logic.
        public List<NameCount> stubBoundScripts = new List<NameCount>();
        public List<NameCount> placeholderScripts = new List<NameCount>();
        public int scriptsMissingInSource;
        public List<string> ambiguousScripts = new List<string>();
        public List<string> scriptsNotAddable = new List<string>();
        public List<NameCount> unsupportedComponents = new List<NameCount>();
        public int unsupportedComponentRows;
        public List<NameCount> extraComponents = new List<NameCount>();
        // Placeholder fields stay in the placeholder's JSON instead of being written as properties.
        public int placeholderFieldsKept;

        // Component fields, fields of ScriptableObjects created by this build, and scene settings, counted apart.
        public PropertyCounts componentProperties = new PropertyCounts();
        public PropertyCounts assetProperties = new PropertyCounts();
        public PropertyCounts sceneSettingProperties = new PropertyCounts();

        public int referencesToObjects;
        public int referencesToAssets;
        public int referencesNull;
        public int referencesRejected;
        public int referencesUnresolved;
        public int referencesToDroppedComponents;
        public List<NameCount> referencesMissing = new List<NameCount>();
        public List<string> rejectedSamples = new List<string>();

        public int assetsCopied;
        public int assetsReused;
        public int importersConfigured;
        public List<string> importerDifferences = new List<string>();
        public int materialsCreated;
        public int materialsReused;
        public int scriptablesCreated;
        public int scriptablesReused;
        public List<NameCount> scriptablesWithoutScript = new List<NameCount>();
        public List<string> shaderSubstitutions = new List<string>();
        public List<string> materialPropertiesDropped = new List<string>();
        public List<string> sceneSettingsApplied = new List<string>();
        public List<string> settingsChanges = new List<string>();

        public static void Increment(List<NameCount> counts, string name)
        {
            foreach (var entry in counts)
            {
                if (entry.name == name)
                {
                    entry.count++;
                    return;
                }
            }

            counts.Add(new NameCount { name = name, count = 1 });
        }

        public static void Sample(List<string> samples, string value)
        {
            if (samples.Count < SampleLimit && !samples.Contains(value))
            {
                samples.Add(value);
            }
        }

        public string ToJson()
        {
            return JsonUtility.ToJson(this, true);
        }
    }
}
