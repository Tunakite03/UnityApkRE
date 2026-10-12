using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEngine;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor
{
    // Brings a scene's assets into <assetsFolder>/Shared/<importPath>:
    //  - file: the exported file is copied; a newly copied image gets the original sprite and texture settings;
    //  - material: created with the original shader (or a URP substitute) and exactly the saved properties and keywords;
    //  - scriptable: a ScriptableObject of the original script type, filled from the original fields after all
    //    assets exist, so assets may reference each other. Without its script type it is not created and
    //    references to it are reported as missing.
    // Existing project files with the same bytes, and existing materials and ScriptableObjects of the expected type, are
    // reused and left as they are. Anything else already at a destination stops the build before anything is written.
    // Every path this build creates is appended to `created`, so a failed build can remove exactly its own files.
    internal static class SceneAssetImporter
    {
        public const string FileKind = "file";
        public const string MaterialKind = "material";
        public const string ScriptableKind = "scriptable";
        private const string SpriteRenderShader = "Universal Render Pipeline/2D/Sprite-Unlit-Default";
        private const string UiShader = "UI/Default";
        private static Dictionary<string, List<Type>> _scriptableTypes;

        public static void Preflight(SceneSpec spec, string projectRoot, string assetsFolder, SceneBuildReport report)
        {
            foreach (var asset in spec.assets)
            {
                var projectPath = ProjectPath(assetsFolder, asset);
                var destination = Path.Combine(projectRoot, projectPath);
                if (asset.kind == FileKind)
                {
                    var source = Path.Combine(spec.sourceRoot, asset.path);
                    if (!File.Exists(source))
                    {
                        throw new SceneBuildException($"Exported asset is missing from the package output; path={source}");
                    }

                    if (File.Exists(destination) && !SameBytes(source, destination))
                    {
                        throw new SceneBuildException($"Project file differs from the exported asset and is not overwritten; path={projectPath}");
                    }
                }
                else if (File.Exists(destination))
                {
                    // AssetDatabase.CreateAsset would replace whatever is there, so only an asset of the expected type is reused.
                    var existing = AssetDatabase.LoadMainAssetAtPath(projectPath);
                    var expected = asset.kind == MaterialKind ? typeof(Material) : ResolveScriptable(asset, report);
                    if (existing == null || (expected != null && existing.GetType() != expected) ||
                        (asset.kind == ScriptableKind && !(existing is ScriptableObject)))
                    {
                        throw new SceneBuildException(
                            $"Project file at an asset destination is not the expected {expected?.FullName ?? asset.kind}; it is not overwritten; path={projectPath}");
                    }
                }
            }
        }

        // Returns the Unity object for each spec asset path. A scriptable entry whose script is not in this project maps
        // to null, which the property writer reports as a missing reference.
        public static Dictionary<string, Object> Import(SceneSpec spec, string projectRoot, string assetsFolder, SceneBuildReport report,
            List<string> created)
        {
            var files = spec.assets.Where(a => a.kind == FileKind).ToList();
            var copied = new HashSet<string>(StringComparer.Ordinal);
            foreach (var asset in files)
            {
                var projectPath = ProjectPath(assetsFolder, asset);
                var destination = Path.Combine(projectRoot, projectPath);
                if (File.Exists(destination))
                {
                    report.assetsReused++;
                    continue;
                }

                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(Path.Combine(spec.sourceRoot, asset.path), destination);
                created.Add(projectPath);
                copied.Add(asset.path);
                report.assetsCopied++;
            }

            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            var loaded = new Dictionary<string, Object>(StringComparer.Ordinal);
            foreach (var asset in files)
            {
                var projectPath = ProjectPath(assetsFolder, asset);
                ConfigureImporter(asset, projectPath, copied.Contains(asset.path), report);
                loaded[asset.path] = Load(asset, projectPath);
            }

            foreach (var material in spec.assets.Where(a => a.kind == MaterialKind))
            {
                loaded[material.path] = CreateMaterial(material, projectRoot, ProjectPath(assetsFolder, material), loaded, report, created);
            }

            var withoutScript = CreateScriptables(spec, projectRoot, assetsFolder, loaded, report, created);
            AssetDatabase.SaveAssets();

            // Saving can reimport the new assets and replace the in-memory objects created above, which then read as
            // destroyed. The scene is wired to the assets as they are now on disk.
            foreach (var asset in spec.assets.Where(a => a.kind != FileKind && !withoutScript.Contains(a.path)))
            {
                var projectPath = ProjectPath(assetsFolder, asset);
                var current = asset.kind == MaterialKind
                    ? (Object)AssetDatabase.LoadAssetAtPath<Material>(projectPath)
                    : AssetDatabase.LoadAssetAtPath<ScriptableObject>(projectPath);
                if (current == null)
                {
                    throw new SceneBuildException($"Created asset did not load back from disk; path={projectPath}");
                }

                loaded[asset.path] = current;
            }

            return loaded;
        }

        private static HashSet<string> CreateScriptables(SceneSpec spec, string projectRoot, string assetsFolder, Dictionary<string, Object> loaded,
            SceneBuildReport report, List<string> created)
        {
            var filled = new List<(AssetSpec Spec, ScriptableObject Asset)>();
            var withoutScript = new HashSet<string>(StringComparer.Ordinal);
            foreach (var scriptable in spec.assets.Where(a => a.kind == ScriptableKind))
            {
                var projectPath = ProjectPath(assetsFolder, scriptable);
                var existing = AssetDatabase.LoadAssetAtPath<ScriptableObject>(projectPath);
                if (existing != null)
                {
                    report.scriptablesReused++;
                    loaded[scriptable.path] = existing;
                    continue;
                }

                var type = ResolveScriptable(scriptable, report);
                if (type == null)
                {
                    SceneBuildReport.Increment(report.scriptablesWithoutScript, ScriptName(scriptable));
                    report.assetProperties.notWritten += scriptable.props.Length;
                    loaded[scriptable.path] = null;
                    withoutScript.Add(scriptable.path);
                    continue;
                }

                if (StubAssemblies.Contains(type))
                {
                    SceneBuildReport.Increment(report.stubBoundScripts, type.FullName + " (asset)");
                }

                var instance = ScriptableObject.CreateInstance(type);
                if (instance == null)
                {
                    throw new SceneBuildException($"Unity could not create the ScriptableObject; type={type.FullName}, path={projectPath}");
                }

                instance.name = scriptable.name;
                Directory.CreateDirectory(Path.GetDirectoryName(Path.Combine(projectRoot, projectPath)));
                AssetDatabase.CreateAsset(instance, projectPath);
                created.Add(projectPath);
                loaded[scriptable.path] = instance;
                filled.Add((scriptable, instance));
            }

            // Fields last: a ScriptableObject may reference any asset of the table, including other ScriptableObjects.
            var noObjects = new Dictionary<string, Object>();
            foreach (var (scriptable, instance) in filled)
            {
                using (var serialized = new SerializedObject(instance))
                {
                    PropertyApplier.Apply(serialized, scriptable.props, noObjects, loaded, report, report.assetProperties, "asset " + scriptable.scriptClass);
                    serialized.ApplyModifiedPropertiesWithoutUndo();
                }

                EditorUtility.SetDirty(instance);
                report.scriptablesCreated++;
            }

            return withoutScript;
        }

        internal static string ProjectPath(string assetsFolder, AssetSpec asset)
        {
            return assetsFolder + "/Shared/" + asset.importPath.Substring("shared/".Length);
        }

        private static string ScriptName(AssetSpec asset)
        {
            var name = string.IsNullOrEmpty(asset.scriptNamespace) ? asset.scriptClass : asset.scriptNamespace + "." + asset.scriptClass;
            return string.IsNullOrEmpty(name) ? "(no script)" : name;
        }

        private static bool SameBytes(string left, string right)
        {
            using var first = File.OpenRead(left);
            using var second = File.OpenRead(right);
            if (first.Length != second.Length)
            {
                return false;
            }

            var a = new byte[81920];
            var b = new byte[81920];
            int read;
            while ((read = first.Read(a, 0, a.Length)) > 0)
            {
                var offset = 0;
                while (offset < read)
                {
                    var got = second.Read(b, offset, read - offset);
                    if (got == 0)
                    {
                        return false;
                    }

                    offset += got;
                }

                if (!a.AsSpan(0, read).SequenceEqual(b.AsSpan(0, read)))
                {
                    return false;
                }
            }

            return true;
        }

        // The unique ScriptableObject type with the spec's full name, or null (reported) when there is none or several.
        private static Type ResolveScriptable(AssetSpec asset, SceneBuildReport report)
        {
            if (asset.kind != ScriptableKind || string.IsNullOrEmpty(asset.scriptClass))
            {
                return null;
            }

            if (_scriptableTypes == null)
            {
                _scriptableTypes = new Dictionary<string, List<Type>>(StringComparer.Ordinal);
                foreach (var candidate in TypeCache.GetTypesDerivedFrom<ScriptableObject>())
                {
                    if (candidate.IsAbstract || candidate.IsGenericTypeDefinition || candidate.FullName == null)
                    {
                        continue;
                    }

                    if (!_scriptableTypes.TryGetValue(candidate.FullName, out var list))
                    {
                        _scriptableTypes[candidate.FullName] = list = new List<Type>();
                    }

                    list.Add(candidate);
                }
            }

            var fullName = ScriptName(asset);
            if (!_scriptableTypes.TryGetValue(fullName, out var matches))
            {
                return null;
            }

            if (matches.Count > 1)
            {
                SceneBuildReport.Sample(report.ambiguousScripts, fullName + " (asset)");
                return null;
            }

            return matches[0];
        }

        // Applies the original sprite and texture settings to a newly copied image. A file that was already in the
        // project keeps its importer, and any difference is reported instead.
        internal static void ConfigureImporter(AssetSpec asset, string projectPath, bool copiedNow, SceneBuildReport report)
        {
            if (asset.className != "Sprite" && asset.className != "Texture2D")
            {
                return;
            }

            var importer = AssetImporter.GetAtPath(projectPath) as TextureImporter;
            if (importer == null || AssetDatabase.LoadAssetAtPath<Texture2D>(projectPath) == null)
            {
                throw new SceneBuildException($"Image did not import as a texture; path={projectPath}");
            }

            var settings = new TextureImporterSettings();
            importer.ReadTextureSettings(settings);
            var platform = importer.GetDefaultPlatformTextureSettings();
            var current = Describe(settings, platform);
            if (asset.className == "Sprite")
            {
                var border = new Vector4(asset.borderLeft, asset.borderBottom, asset.borderRight, asset.borderTop);
                settings.textureType = TextureImporterType.Sprite;
                settings.spriteMode = (int)SpriteImportMode.Single;
                settings.spritePixelsPerUnit = asset.pixelsPerUnit;
                settings.spriteAlignment = (int)SpriteAlignment.Custom;
                settings.spritePivot = new Vector2(asset.pivotX, asset.pivotY);
                settings.spriteBorder = border;
                // A sliced or tiled sprite needs a full-rect mesh; Unity warns and draws it wrongly otherwise.
                settings.spriteMeshType = border == Vector4.zero ? SpriteMeshType.Tight : SpriteMeshType.FullRect;
                settings.alphaIsTransparency = true;
            }

            // The original texture's sampling settings; -1 means the type tree did not record them.
            if (asset.filterMode >= 0)
            {
                settings.filterMode = (FilterMode)asset.filterMode;
            }

            if (asset.wrapU >= 0)
            {
                settings.wrapModeU = (TextureWrapMode)asset.wrapU;
            }

            if (asset.wrapV >= 0)
            {
                settings.wrapModeV = (TextureWrapMode)asset.wrapV;
            }

            settings.mipmapEnabled = asset.mipmaps;
            settings.readable = asset.readable;
            if (asset.alphaOnly)
            {
                // A single-channel original (for example a TMP SDF atlas) keeps its alpha as the only channel.
                settings.alphaSource = TextureImporterAlphaSource.FromInput;
                platform.format = TextureImporterFormat.Alpha8;
                platform.textureCompression = TextureImporterCompression.Uncompressed;
            }

            importer.GetSourceTextureWidthAndHeight(out var width, out var height);
            var maxSize = 2048;
            while (maxSize < Math.Max(width, height) && maxSize < 16384)
            {
                maxSize *= 2;
            }

            platform.maxTextureSize = maxSize;
            var wanted = Describe(settings, platform);
            if (wanted == current)
            {
                return;
            }

            if (!copiedNow)
            {
                SceneBuildReport.Sample(report.importerDifferences, $"{projectPath}: project [{current}] original [{wanted}]");
                return;
            }

            importer.SetTextureSettings(settings);
            importer.SetPlatformTextureSettings(platform);
            importer.SaveAndReimport();
            report.importersConfigured++;
        }

        private static string Describe(TextureImporterSettings settings, TextureImporterPlatformSettings platform)
        {
            string Number(float value) => value.ToString("R", CultureInfo.InvariantCulture);
            return string.Join("|", settings.textureType, settings.spriteMode, Number(settings.spritePixelsPerUnit), settings.spriteAlignment,
                Number(settings.spritePivot.x), Number(settings.spritePivot.y), Number(settings.spriteBorder.x), Number(settings.spriteBorder.y),
                Number(settings.spriteBorder.z), Number(settings.spriteBorder.w), settings.spriteMeshType, settings.alphaIsTransparency,
                settings.filterMode, settings.wrapModeU, settings.wrapModeV, settings.mipmapEnabled, settings.readable, settings.alphaSource,
                platform.format, platform.textureCompression, platform.maxTextureSize);
        }

        private static Object Load(AssetSpec asset, string projectPath)
        {
            Object loaded = asset.className switch
            {
                "Sprite" => AssetDatabase.LoadAssetAtPath<Sprite>(projectPath),
                "Texture2D" => AssetDatabase.LoadAssetAtPath<Texture2D>(projectPath),
                "Font" => AssetDatabase.LoadAssetAtPath<Font>(projectPath),
                "AudioClip" => AssetDatabase.LoadAssetAtPath<AudioClip>(projectPath),
                "TextAsset" => AssetDatabase.LoadAssetAtPath<TextAsset>(projectPath),
                "Mesh" => AssetDatabase.LoadAllAssetsAtPath(projectPath).OfType<Mesh>().FirstOrDefault(),
                _ => throw new SceneBuildException($"Scene spec asset class is not importable; class={asset.className}, path={asset.path}"),
            };
            if (loaded == null)
            {
                throw new SceneBuildException($"Imported asset did not load as {asset.className}; path={projectPath}");
            }

            return loaded;
        }

        private static Material CreateMaterial(AssetSpec spec, string projectRoot, string projectPath, Dictionary<string, Object> loaded,
            SceneBuildReport report, List<string> created)
        {
            var existing = AssetDatabase.LoadAssetAtPath<Material>(projectPath);
            if (existing != null)
            {
                report.materialsReused++;
                if (existing.shader.name != spec.shaderName)
                {
                    report.shaderSubstitutions.Add($"{projectPath}: '{spec.shaderName}' -> '{existing.shader.name}' (reused)");
                }

                return existing;
            }

            var shader = ResolveShader(spec, projectPath, report, out var substituted);
            var material = MaterialFactory.Create(spec, shader, substituted, loaded, report);
            Directory.CreateDirectory(Path.GetDirectoryName(Path.Combine(projectRoot, projectPath)));
            AssetDatabase.CreateAsset(material, projectPath);
            created.Add(projectPath);
            report.materialsCreated++;
            return material;
        }

        // The original shader when this project has it; otherwise a URP sprite or UI shader, recorded as a substitution.
        private static Shader ResolveShader(AssetSpec spec, string projectPath, SceneBuildReport report, out bool substituted)
        {
            var original = string.IsNullOrEmpty(spec.shaderName) ? null : Shader.Find(spec.shaderName);
            if (original != null && original.isSupported)
            {
                substituted = false;
                return original;
            }

            var ui = spec.shaderName.StartsWith("UI/", StringComparison.Ordinal) || spec.shaderName.StartsWith("FairyGUI/", StringComparison.Ordinal) ||
                     spec.shaderName.StartsWith("TextMeshPro/", StringComparison.Ordinal);
            var fallbackName = ui ? UiShader : SpriteRenderShader;
            var fallback = Shader.Find(fallbackName);
            if (fallback == null)
            {
                throw new SceneBuildException($"Substitute shader is not available in this project; shader={fallbackName}, material={projectPath}");
            }

            report.shaderSubstitutions.Add($"{projectPath}: '{spec.shaderName}' -> '{fallbackName}'");
            substituted = true;
            return fallback;
        }
    }

    // Builds an in-memory material from a spec entry: every saved property the shader declares, exactly the saved keywords
    // (a new material starts with the shader's default keywords, which are cleared), and the saved render queue unless
    // the shader is a substitute. Everything that cannot be carried over is reported.
    internal static class MaterialFactory
    {
        public static Material Create(AssetSpec spec, Shader shader, bool substituted, IReadOnlyDictionary<string, Object> loaded, SceneBuildReport report)
        {
            var material = new Material(shader) { name = spec.name };
            foreach (var texture in spec.textures)
            {
                if (!material.HasProperty(texture.name))
                {
                    report.materialPropertiesDropped.Add($"{spec.importPath}: texture {texture.name} not on shader '{shader.name}'");
                    continue;
                }

                if (!string.IsNullOrEmpty(texture.missing))
                {
                    report.materialPropertiesDropped.Add($"{spec.importPath}: texture {texture.name} is a {texture.missing} that was not exported");
                }
                else if (!string.IsNullOrEmpty(texture.path))
                {
                    if (!loaded.TryGetValue(texture.path, out var value) || !(value is Texture) || value == null)
                    {
                        throw new SceneBuildException($"Material texture was not imported or was destroyed; material={spec.path}, texture={texture.path}");
                    }

                    material.SetTexture(texture.name, (Texture)value);
                }

                material.SetTextureScale(texture.name, new Vector2(texture.scaleX, texture.scaleY));
                material.SetTextureOffset(texture.name, new Vector2(texture.offsetX, texture.offsetY));
            }

            foreach (var value in spec.floats)
            {
                if (material.HasProperty(value.name))
                {
                    material.SetFloat(value.name, (float)value.value);
                }
                else
                {
                    report.materialPropertiesDropped.Add($"{spec.importPath}: float {value.name} not on shader '{shader.name}'");
                }
            }

            foreach (var value in spec.ints)
            {
                if (material.HasProperty(value.name))
                {
                    material.SetInteger(value.name, checked((int)value.value));
                }
                else
                {
                    report.materialPropertiesDropped.Add($"{spec.importPath}: int {value.name} not on shader '{shader.name}'");
                }
            }

            foreach (var color in spec.colors)
            {
                if (material.HasProperty(color.name))
                {
                    material.SetColor(color.name, new Color(color.r, color.g, color.b, color.a));
                }
                else
                {
                    report.materialPropertiesDropped.Add($"{spec.importPath}: color {color.name} not on shader '{shader.name}'");
                }
            }

            var declared = new HashSet<string>(shader.keywordSpace.keywordNames, StringComparer.Ordinal);
            foreach (var keyword in spec.keywords.Where(k => !declared.Contains(k)))
            {
                report.materialPropertiesDropped.Add($"{spec.importPath}: keyword {keyword} not declared by shader '{shader.name}'");
            }

            material.shaderKeywords = spec.keywords;
            if (spec.renderQueue >= 0 && !substituted)
            {
                material.renderQueue = spec.renderQueue;
            }
            else if (spec.renderQueue >= 0)
            {
                report.materialPropertiesDropped.Add($"{spec.importPath}: render queue {spec.renderQueue} not applied to substitute shader '{shader.name}'");
            }

            return material;
        }
    }
}
