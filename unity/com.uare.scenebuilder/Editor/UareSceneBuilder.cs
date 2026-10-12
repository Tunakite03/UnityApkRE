using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor
{
    // Builds one scene from a uare scene spec and saves it as <assetsFolder>/Scenes/<scene>.unity.
    //
    // Order: validate the spec and every file it needs (no writes) -> new empty scene -> project settings (tags, layers,
    // sorting layers) -> the scene's assets -> GameObjects and components -> serialized properties and references ->
    // scene settings -> active state -> save.
    // An existing scene file is never replaced; delete it first to rebuild. If the scene step fails, the editor is left on
    // an empty untitled scene, not on a half-built one. Built-in components are created by class name. A MonoBehaviour is
    // bound to its original script when exactly one type with the same full name exists in this project and Unity can
    // add it; otherwise a UareMissingScript placeholder keeps its identity and fields. Returns the build report as JSON.
    public static class UareSceneBuilder
    {
        private static Dictionary<string, List<Type>> _scriptTypes;
        private static Dictionary<string, Type> _engineTypes;

        public static string BuildScene(string specPath, string assetsFolder)
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode)
            {
                throw new SceneBuildException("Scenes cannot be built in Play Mode");
            }

            if (string.IsNullOrEmpty(assetsFolder) || !assetsFolder.StartsWith("Assets/", StringComparison.Ordinal) || assetsFolder.EndsWith("/", StringComparison.Ordinal) ||
                assetsFolder.Contains("..") || assetsFolder.Contains("\\") || assetsFolder.Contains("//"))
            {
                throw new SceneBuildException($"assetsFolder must be a folder path under Assets/ without a trailing slash; value={assetsFolder}");
            }

            var spec = SceneSpecReader.Read(specPath);
            var projectRoot = Path.GetDirectoryName(Application.dataPath);
            var scenePath = $"{assetsFolder}/Scenes/{spec.scene}.unity";
            if (File.Exists(Path.Combine(projectRoot, scenePath)))
            {
                throw new SceneBuildException($"Scene already exists and is not replaced; delete it to rebuild; path={scenePath}");
            }

            for (var index = 0; index < SceneManager.sceneCount; index++)
            {
                var open = SceneManager.GetSceneAt(index);
                if (open.isDirty)
                {
                    throw new SceneBuildException($"Open scene has unsaved changes; save or discard them first; scene={open.path}");
                }
            }

            var report = new SceneBuildReport
            {
                scene = spec.scene, scenePath = scenePath, specPath = specPath, scopeId = spec.scopeId, originalPath = spec.originalPath,
                rootOrderBasis = spec.rootOrderBasis,
            };
            SceneAssetImporter.Preflight(spec, projectRoot, assetsFolder, report);
            // The new scene comes first: opening a scene unloads unused assets, which would destroy assets loaded
            // for this build before the scene references them.
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var created = new List<string>();
            var saved = false;
            try
            {
                ProjectSettingsWriter.Apply(spec, report);
                var assets = SceneAssetImporter.Import(spec, projectRoot, assetsFolder, report, created);
                var objects = CreateHierarchy(spec, report);
                foreach (var gameObject in spec.gameObjects)
                {
                    foreach (var component in gameObject.components)
                    {
                        ApplyProperties(component, objects, assets, report);
                    }
                }

                ApplySceneSettings(spec, objects, assets, report);

                // Last, so inactive objects still receive every property and reference first.
                foreach (var gameObject in spec.gameObjects.Where(g => !g.active))
                {
                    ((GameObject)objects[gameObject.id]).SetActive(false);
                }

                Directory.CreateDirectory(Path.GetDirectoryName(Path.Combine(projectRoot, scenePath)));
                if (!EditorSceneManager.SaveScene(scene, scenePath))
                {
                    throw new SceneBuildException($"Unity did not save the scene; path={scenePath}");
                }

                saved = true;
            }
            finally
            {
                if (!saved)
                {
                    // Discard the half-built scene and remove the files this build created, so a retry starts from the
                    // same state instead of reusing half-filled assets. Tag, layer and sorting-layer additions stay;
                    // they are additive and checked for conflicts on every build.
                    EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
                    for (var index = created.Count - 1; index >= 0; index--)
                    {
                        AssetDatabase.DeleteAsset(created[index]);
                    }
                }
            }

            return report.ToJson();
        }

        private static Dictionary<string, Object> CreateHierarchy(SceneSpec spec, SceneBuildReport report)
        {
            var objects = new Dictionary<string, Object>(StringComparer.Ordinal);
            foreach (var node in spec.gameObjects)
            {
                var rect = node.components.Any(c => c.className == "RectTransform");
                var gameObject = rect ? new GameObject(node.name, typeof(RectTransform)) : new GameObject(node.name);
                if (!string.IsNullOrEmpty(node.parentId))
                {
                    if (!objects.TryGetValue(node.parentId, out var parent))
                    {
                        throw new SceneBuildException($"Parent is not created before its child; scene={spec.scene}, object={node.id}");
                    }

                    gameObject.transform.SetParent(((GameObject)parent).transform, false);
                }

                if (gameObject.transform.GetSiblingIndex() != node.siblingIndex)
                {
                    throw new SceneBuildException($"Sibling order differs from the spec; scene={spec.scene}, object={node.id}");
                }

                gameObject.layer = node.layer;
                gameObject.tag = node.tag;
                objects[node.id] = gameObject;
                report.gameObjects++;

                var claimed = new HashSet<Component>();
                foreach (var component in node.components)
                {
                    var created = CreateComponent(gameObject, component, claimed, report);
                    if (created != null)
                    {
                        objects[component.id] = created;
                        report.components++;
                    }
                }

                // Components Unity added on its own (RequireComponent) that the original object did not have.
                foreach (var extra in gameObject.GetComponents<Component>().Where(c => c != null && !claimed.Contains(c)))
                {
                    SceneBuildReport.Increment(report.extraComponents, extra.GetType().Name);
                }
            }

            return objects;
        }

        private static Component CreateComponent(GameObject gameObject, ComponentSpec spec, HashSet<Component> claimed, SceneBuildReport report)
        {
            if (spec.className == "MonoBehaviour")
            {
                var script = ResolveScript(spec, report);
                if (script != null)
                {
                    var bound = TryClaim(gameObject, script, claimed);
                    if (bound != null)
                    {
                        var stub = StubAssemblies.Contains(script);
                        SceneBuildReport.Increment(stub ? report.stubBoundScripts : report.boundScripts, script.FullName);
                        SceneBuildReport.Increment(report.componentClasses, (stub ? "MonoBehaviour (stub " : "MonoBehaviour (") + script.FullName + ")");
                        return bound;
                    }

                    // Unity refused the type (editor-only assembly, DisallowMultipleComponent, RequireComponent conflict).
                    SceneBuildReport.Sample(report.scriptsNotAddable, script.FullName + " on " + gameObject.name);
                    SceneBuildReport.Increment(report.placeholderScripts, script.FullName);
                }

                var placeholder = gameObject.AddComponent<UareMissingScript>();
                using (var fields = new SerializedObject(placeholder))
                {
                    fields.FindProperty("scriptClass").stringValue = spec.scriptClass;
                    fields.FindProperty("scriptNamespace").stringValue = spec.scriptNamespace;
                    fields.FindProperty("scriptAssembly").stringValue = spec.scriptAssembly;
                    fields.FindProperty("sourceObjectId").stringValue = spec.id;
                    fields.FindProperty("serializedJson").stringValue = spec.rawJson;
                    fields.ApplyModifiedPropertiesWithoutUndo();
                }

                claimed.Add(placeholder);
                SceneBuildReport.Increment(report.componentClasses, "MonoBehaviour (placeholder)");
                return placeholder;
            }

            var type = ResolveEngineType(spec.className);
            if (type == null)
            {
                SceneBuildReport.Increment(report.unsupportedComponents, spec.className);
                report.unsupportedComponentRows += spec.props.Length;
                return null;
            }

            var component = TryClaim(gameObject, type, claimed);
            if (component == null)
            {
                throw new SceneBuildException($"Unity refused to add a built-in component; object={gameObject.name}, type={type.FullName}");
            }

            SceneBuildReport.Increment(report.componentClasses, spec.className);
            return component;
        }

        // Reuses a component Unity already added (Transform, or one added by RequireComponent) before adding a new one.
        // Returns null when Unity refuses to add the type.
        private static Component TryClaim(GameObject gameObject, Type type, HashSet<Component> claimed)
        {
            var existing = gameObject.GetComponents(type).FirstOrDefault(c => c.GetType() == type && !claimed.Contains(c));
            var component = existing != null ? existing : gameObject.AddComponent(type);
            if (component != null)
            {
                claimed.Add(component);
            }

            return component;
        }

        private static Type ResolveScript(ComponentSpec spec, SceneBuildReport report)
        {
            if (string.IsNullOrEmpty(spec.scriptClass))
            {
                report.scriptsMissingInSource++;
                SceneBuildReport.Increment(report.placeholderScripts, "(no script in the original build)");
                return null;
            }

            var fullName = string.IsNullOrEmpty(spec.scriptNamespace) ? spec.scriptClass : spec.scriptNamespace + "." + spec.scriptClass;
            if (_scriptTypes == null)
            {
                _scriptTypes = new Dictionary<string, List<Type>>(StringComparer.Ordinal);
                foreach (var candidate in TypeCache.GetTypesDerivedFrom<MonoBehaviour>())
                {
                    if (candidate.IsAbstract || candidate.IsGenericTypeDefinition || candidate == typeof(UareMissingScript) || candidate.FullName == null)
                    {
                        continue;
                    }

                    if (!_scriptTypes.TryGetValue(candidate.FullName, out var list))
                    {
                        _scriptTypes[candidate.FullName] = list = new List<Type>();
                    }

                    list.Add(candidate);
                }
            }

            if (_scriptTypes.TryGetValue(fullName, out var matches) && matches.Count == 1)
            {
                return matches[0];
            }

            if (matches != null && matches.Count > 1)
            {
                SceneBuildReport.Sample(report.ambiguousScripts, fullName);
            }

            SceneBuildReport.Increment(report.placeholderScripts, fullName);
            return null;
        }

        // Native components by class name, from Unity's engine modules only. A name defined twice is not guessed.
        private static Type ResolveEngineType(string className)
        {
            if (_engineTypes == null)
            {
                _engineTypes = new Dictionary<string, Type>(StringComparer.Ordinal);
                foreach (var candidate in TypeCache.GetTypesDerivedFrom<Component>())
                {
                    var assembly = candidate.Assembly.GetName().Name;
                    if (candidate.IsAbstract || typeof(MonoBehaviour).IsAssignableFrom(candidate) ||
                        !assembly.StartsWith("UnityEngine", StringComparison.Ordinal))
                    {
                        continue;
                    }

                    _engineTypes[candidate.Name] = _engineTypes.TryGetValue(candidate.Name, out var known) && known != candidate ? null : candidate;
                }
            }

            return _engineTypes.TryGetValue(className, out var type) ? type : null;
        }

        private static void ApplyProperties(ComponentSpec spec, IReadOnlyDictionary<string, Object> objects, IReadOnlyDictionary<string, Object> assets,
            SceneBuildReport report)
        {
            if (!objects.TryGetValue(spec.id, out var target))
            {
                return; // Unsupported component; its rows were counted when it was skipped.
            }

            using var serialized = new SerializedObject(target);
            if (target is UareMissingScript)
            {
                // A placeholder keeps the original fields as JSON; only the enabled flag is a real property.
                var enabled = spec.props.Where(row => row.path == "m_Enabled").ToArray();
                report.placeholderFieldsKept += spec.props.Length - enabled.Length;
                PropertyApplier.Apply(serialized, enabled, objects, assets, report, report.componentProperties, ClassLabel(spec));
            }
            else
            {
                PropertyApplier.Apply(serialized, spec.props, objects, assets, report, report.componentProperties, ClassLabel(spec));
            }

            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        // RenderSettings and LightmapSettings belong to the active scene. Unity exposes the objects only internally,
        // so they are read through reflection and fail loudly if this Unity version renamed them.
        private static void ApplySceneSettings(SceneSpec spec, IReadOnlyDictionary<string, Object> objects, IReadOnlyDictionary<string, Object> assets,
            SceneBuildReport report)
        {
            foreach (var setting in spec.sceneSettings)
            {
                Object target;
                switch (setting.className)
                {
                    case "RenderSettings":
                        target = InternalSettingsObject(typeof(RenderSettings), "GetRenderSettings");
                        break;
                    case "LightmapSettings":
                        target = InternalSettingsObject(typeof(LightmapEditorSettings), "GetLightmapSettings");
                        break;
                    default:
                        throw new SceneBuildException($"Unknown scene settings class in spec; class={setting.className}");
                }

                using var serialized = new SerializedObject(target);
                var counts = new PropertyCounts();
                PropertyApplier.Apply(serialized, setting.props, objects, assets, report, counts, setting.className);
                serialized.ApplyModifiedPropertiesWithoutUndo();
                report.sceneSettingsApplied.Add($"{setting.className}: {counts.applied} of {setting.props.Length} rows applied, {counts.unmatched} without an editor property");
                Accumulate(report.sceneSettingProperties, counts);
            }
        }

        private static void Accumulate(PropertyCounts total, PropertyCounts part)
        {
            total.applied += part.applied;
            total.unmatched += part.unmatched;
            total.typeMismatch += part.typeMismatch;
            total.notWritten += part.notWritten;
            foreach (var sample in part.unmatchedSamples)
            {
                SceneBuildReport.Sample(total.unmatchedSamples, sample);
            }

            foreach (var sample in part.mismatchSamples)
            {
                SceneBuildReport.Sample(total.mismatchSamples, sample);
            }
        }

        private static Object InternalSettingsObject(Type owner, string method)
        {
            var getter = owner.GetMethod(method, BindingFlags.NonPublic | BindingFlags.Static, null, Type.EmptyTypes, null);
            Object settings;
            try
            {
                settings = getter?.Invoke(null, null) as Object;
            }
            catch (TargetInvocationException error)
            {
                throw new SceneBuildException($"{owner.Name}.{method} failed: {error.InnerException?.Message ?? error.Message}");
            }

            if (settings == null)
            {
                throw new SceneBuildException($"This Unity version does not expose {owner.Name}.{method}; scene settings cannot be applied");
            }

            return settings;
        }

        private static string ClassLabel(ComponentSpec spec)
        {
            return string.IsNullOrEmpty(spec.scriptClass) ? spec.className : spec.scriptClass;
        }
    }
}
