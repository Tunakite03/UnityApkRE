using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;

namespace Uare.SceneBuilder.Editor
{
    // Field names match the JSON written by `uare scene-spec` (schema uare.scene-spec, version 2).
    [Serializable]
    internal sealed class SceneSpec
    {
        public string schema;
        public int version;
        public string scene;
        public string scopeId;
        public string originalPath;
        public string source;
        public string serializedFile;
        public string sourceRoot;
        public string rootOrderBasis;
        public string[] tags;
        public string[] layers;
        public SortingLayerSpec[] sortingLayers;
        public GameObjectSpec[] gameObjects;
        public SceneSettingSpec[] sceneSettings;
        public AssetSpec[] assets;
    }

    [Serializable]
    internal sealed class SortingLayerSpec
    {
        public string name;
        public long uniqueId;
    }

    [Serializable]
    internal sealed class GameObjectSpec
    {
        public string id;
        public string name;
        public string parentId;
        public int siblingIndex;
        public bool active;
        public int layer;
        public string tag;
        public ComponentSpec[] components;
    }

    [Serializable]
    internal sealed class ComponentSpec
    {
        public string id;
        public string className;
        public string scriptClass;
        public string scriptNamespace;
        public string scriptAssembly;
        public string rawJson;
        public PropertySpec[] props;
    }

    // RenderSettings or LightmapSettings of the original scene.
    [Serializable]
    internal sealed class SceneSettingSpec
    {
        public string id;
        public string className;
        public PropertySpec[] props;
    }

    // kind: number | string | arraySize | ref. refKind (for ref): object | asset | null | missing | unresolved.
    // A number row with nonempty text is a non-finite float spelled "NaN", "Infinity" or "-Infinity".
    [Serializable]
    internal sealed class PropertySpec
    {
        public string path;
        public string kind;
        public double number;
        public string text;
        public string refKind;
        public string refTarget;
        public string refClass;
    }

    // kind: file (copied), material (created from its properties) or scriptable (ScriptableObject created from its fields).
    // path is the source under sourceRoot; importPath is where the asset goes under <assetsFolder>/Shared.
    [Serializable]
    internal sealed class AssetSpec
    {
        public string path;
        public string importPath;
        public string kind;
        public string className;
        public string name;
        public float pixelsPerUnit;
        public float pivotX;
        public float pivotY;
        public float borderLeft;
        public float borderBottom;
        public float borderRight;
        public float borderTop;
        public int filterMode;
        public int wrapU;
        public int wrapV;
        public bool mipmaps;
        public bool readable;
        public bool alphaOnly;
        public string shaderName;
        public int renderQueue;
        public string[] keywords;
        public MaterialTextureSpec[] textures;
        public NamedValueSpec[] floats;
        public NamedValueSpec[] ints;
        public NamedColorSpec[] colors;
        public string scriptClass;
        public string scriptNamespace;
        public string scriptAssembly;
        public PropertySpec[] props;
    }

    [Serializable]
    internal sealed class MaterialTextureSpec
    {
        public string name;
        public string path;
        public string missing;
        public float scaleX;
        public float scaleY;
        public float offsetX;
        public float offsetY;
    }

    [Serializable]
    internal sealed class NamedValueSpec
    {
        public string name;
        public double value;
    }

    [Serializable]
    internal sealed class NamedColorSpec
    {
        public string name;
        public float r;
        public float g;
        public float b;
        public float a;
    }

    internal static class SceneSpecReader
    {
        private const string Schema = "uare.scene-spec";
        private const int Version = 2;

        private static readonly HashSet<string> RowKinds = new HashSet<string> { "number", "string", "arraySize", "ref" };
        private static readonly HashSet<string> ReferenceKinds = new HashSet<string> { "object", "asset", "null", "missing", "unresolved" };
        private static readonly HashSet<string> AssetKinds = new HashSet<string>
        {
            SceneAssetImporter.FileKind, SceneAssetImporter.MaterialKind, SceneAssetImporter.ScriptableKind,
        };

        // Extensions Unity imports as each copied class.
        private static readonly Dictionary<string, HashSet<string>> FileExtensions = new Dictionary<string, HashSet<string>>
        {
            { "Sprite", new HashSet<string> { ".png" } },
            { "Texture2D", new HashSet<string> { ".png" } },
            { "Mesh", new HashSet<string> { ".obj" } },
            { "Font", new HashSet<string> { ".ttf", ".otf" } },
            { "AudioClip", new HashSet<string> { ".wav", ".ogg", ".mp3" } },
            { "TextAsset", new HashSet<string> { ".txt", ".bytes", ".json", ".csv", ".xml", ".yaml", ".html", ".htm", ".fnt" } },
        };

        public static SceneSpec Read(string specPath)
        {
            if (!File.Exists(specPath))
            {
                throw new SceneBuildException($"Scene spec not found; path={specPath}");
            }

            var spec = JsonUtility.FromJson<SceneSpec>(File.ReadAllText(specPath));
            if (spec == null || spec.schema != Schema || spec.version != Version)
            {
                throw new SceneBuildException($"Unsupported scene spec; expected {Schema} v{Version}; path={specPath}");
            }

            if (string.IsNullOrEmpty(spec.scene) || spec.scene.IndexOfAny(new[] { '/', '\\' }) >= 0 || spec.scene.Contains(".."))
            {
                throw new SceneBuildException($"Scene spec has an unsafe scene name; path={specPath}");
            }

            if (string.IsNullOrEmpty(spec.sourceRoot) || !Directory.Exists(spec.sourceRoot))
            {
                throw new SceneBuildException($"Scene spec source root does not exist; scene={spec.scene}, sourceRoot={spec.sourceRoot}");
            }

            if (spec.gameObjects == null || spec.assets == null || spec.tags == null || spec.layers == null || spec.sortingLayers == null ||
                spec.sceneSettings == null)
            {
                throw new SceneBuildException($"Scene spec lacks required sections; scene={spec.scene}");
            }

            var assetPaths = new HashSet<string>(StringComparer.Ordinal);
            var importPaths = new HashSet<string>(StringComparer.Ordinal);
            foreach (var asset in spec.assets)
            {
                ValidateAsset(spec.scene, asset);
                if (!assetPaths.Add(asset.path) || !importPaths.Add(asset.importPath))
                {
                    throw new SceneBuildException($"Scene spec lists an asset path twice; scene={spec.scene}, path={asset.path}, importPath={asset.importPath}");
                }
            }

            foreach (var asset in spec.assets)
            {
                ValidateRows(spec.scene, asset.props, assetPaths);
                foreach (var texture in asset.textures)
                {
                    if (!string.IsNullOrEmpty(texture.path) && !assetPaths.Contains(texture.path))
                    {
                        throw new SceneBuildException($"Material texture is outside the asset table; scene={spec.scene}, material={asset.path}, texture={texture.path}");
                    }
                }
            }

            ValidateHierarchy(spec, assetPaths);

            foreach (var setting in spec.sceneSettings)
            {
                ValidateRows(spec.scene, setting.props, assetPaths);
            }

            return spec;
        }

        // Ids are unique, every parent precedes its children, and sibling indexes count up from 0 under each parent,
        // which is the order the builder creates objects in.
        private static void ValidateHierarchy(SceneSpec spec, HashSet<string> assetPaths)
        {
            var ids = new HashSet<string>(StringComparer.Ordinal);
            var nextSibling = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var node in spec.gameObjects)
            {
                if (string.IsNullOrEmpty(node.id) || !ids.Add(node.id) || node.components == null)
                {
                    throw new SceneBuildException($"Scene spec GameObject has a missing or repeated id, or no component list; scene={spec.scene}, id={node.id}");
                }

                var parent = node.parentId ?? string.Empty;
                if (parent.Length > 0 && !ids.Contains(parent))
                {
                    throw new SceneBuildException($"Scene spec lists a child before its parent; scene={spec.scene}, object={node.id}, parent={parent}");
                }

                nextSibling.TryGetValue(parent, out var expected);
                if (node.siblingIndex != expected)
                {
                    throw new SceneBuildException($"Scene spec sibling index is out of order; scene={spec.scene}, object={node.id}, index={node.siblingIndex}, expected={expected}");
                }

                nextSibling[parent] = expected + 1;
                foreach (var component in node.components)
                {
                    if (string.IsNullOrEmpty(component.id) || !ids.Add(component.id) || component.props == null)
                    {
                        throw new SceneBuildException($"Scene spec component has a missing or repeated id, or no property list; scene={spec.scene}, id={component.id}");
                    }

                    ValidateRows(spec.scene, component.props, assetPaths);
                }
            }
        }

        private static void ValidateAsset(string scene, AssetSpec asset)
        {
            foreach (var path in new[] { asset.path, asset.importPath })
            {
                if (string.IsNullOrEmpty(path) || !path.StartsWith("shared/", StringComparison.Ordinal) || path.Contains("..") || path.Contains("\\"))
                {
                    throw new SceneBuildException($"Scene spec has an unsafe asset path; scene={scene}, path={path}");
                }
            }

            if (!AssetKinds.Contains(asset.kind ?? string.Empty))
            {
                throw new SceneBuildException($"Scene spec asset has an unknown kind; scene={scene}, kind={asset.kind}, path={asset.path}");
            }

            var extension = Path.GetExtension(asset.importPath).ToLowerInvariant();
            var importable = asset.kind == SceneAssetImporter.MaterialKind ? extension == ".mat"
                : asset.kind == SceneAssetImporter.ScriptableKind ? extension == ".asset"
                : FileExtensions.TryGetValue(asset.className ?? string.Empty, out var extensions) && extensions.Contains(extension);
            if (!importable)
            {
                throw new SceneBuildException($"Scene spec asset cannot be imported as its class; scene={scene}, class={asset.className}, importPath={asset.importPath}");
            }

            if (asset.keywords == null || asset.textures == null || asset.floats == null || asset.ints == null || asset.colors == null || asset.props == null)
            {
                throw new SceneBuildException($"Scene spec asset lacks required sections; scene={scene}, path={asset.path}");
            }
        }

        private static void ValidateRows(string scene, PropertySpec[] rows, HashSet<string> assetPaths)
        {
            if (rows == null)
            {
                throw new SceneBuildException($"Scene spec has an object without a property list; scene={scene}");
            }

            foreach (var row in rows)
            {
                if (!RowKinds.Contains(row.kind) || (row.kind == "ref" && !ReferenceKinds.Contains(row.refKind)))
                {
                    throw new SceneBuildException($"Scene spec row has an unknown kind; scene={scene}, path={row.path}, kind={row.kind}, refKind={row.refKind}");
                }

                if (row.kind == "number" && !string.IsNullOrEmpty(row.text) && !SerializedPropertyWriter.NonFiniteNumbers.ContainsKey(row.text))
                {
                    throw new SceneBuildException($"Scene spec number row has an unknown non-finite spelling; scene={scene}, path={row.path}, text={row.text}");
                }

                if (row.kind == "ref" && row.refKind == "asset" && !assetPaths.Contains(row.refTarget))
                {
                    throw new SceneBuildException($"Scene spec references an asset outside its asset table; scene={scene}, asset={row.refTarget}");
                }
            }
        }
    }

    public sealed class SceneBuildException : Exception
    {
        public SceneBuildException(string message) : base(message)
        {
        }
    }
}
