using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using UnityEngine.TestTools;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor.Tests
{
    public sealed class AssetImportTests
    {
        private const string Folder = "Assets/UareBuilderTests";
        private string _sourceRoot;

        [SetUp]
        public void CreateFolders()
        {
            Directory.CreateDirectory(Folder);
            AssetDatabase.Refresh();
            _sourceRoot = Path.Combine(Path.GetTempPath(), "uare-builder-tests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_sourceRoot);
        }

        [TearDown]
        public void DeleteFolders()
        {
            LogAssert.ignoreFailingMessages = false;
            AssetDatabase.DeleteAsset(Folder);
            Directory.Delete(_sourceRoot, true);
        }

        private static AssetSpec Entry(string className, string path)
        {
            return new AssetSpec
            {
                path = path, importPath = path, kind = "file", className = className, name = Path.GetFileNameWithoutExtension(path),
                pixelsPerUnit = 100, pivotX = 0.5f, pivotY = 0.5f, filterMode = -1, wrapU = -1, wrapV = -1, shaderName = "", renderQueue = -1,
                keywords = Array.Empty<string>(), textures = Array.Empty<MaterialTextureSpec>(), floats = Array.Empty<NamedValueSpec>(),
                ints = Array.Empty<NamedValueSpec>(), colors = Array.Empty<NamedColorSpec>(), scriptClass = "", scriptNamespace = "",
                scriptAssembly = "", props = Array.Empty<PropertySpec>(),
            };
        }

        private static string WritePng(string projectPath, int width, int height)
        {
            var texture = new Texture2D(width, height, TextureFormat.RGBA32, false);
            File.WriteAllBytes(projectPath, texture.EncodeToPNG());
            Object.DestroyImmediate(texture);
            AssetDatabase.ImportAsset(projectPath, ImportAssetOptions.ForceSynchronousImport);
            return projectPath;
        }

        [Test]
        public void MaterialKeywordsAreExactlyTheSavedOnes()
        {
            var shader = Shader.Find("Skybox/Procedural");
            Assert.IsNotNull(shader, "built-in Skybox/Procedural shader is required for this test");
            var spec = Entry("Material", "shared/materials/test/Sky.json");
            spec.kind = "material";
            spec.keywords = new[] { "_SUNDISK_SIMPLE", "UARE_UNDECLARED_KEYWORD" };
            var report = new SceneBuildReport();

            var material = MaterialFactory.Create(spec, shader, false, new Dictionary<string, Object>(), report);
            try
            {
                CollectionAssert.AreEquivalent(new[] { "_SUNDISK_SIMPLE", "UARE_UNDECLARED_KEYWORD" }, material.shaderKeywords);
                Assert.IsFalse(material.IsKeywordEnabled("_SUNDISK_HIGH_QUALITY"), "a default keyword of the shader stayed on");
                Assert.IsTrue(report.materialPropertiesDropped.Any(entry => entry.Contains("UARE_UNDECLARED_KEYWORD")));
            }
            finally
            {
                Object.DestroyImmediate(material);
            }
        }

        [Test]
        public void RenderQueueIsNotForcedOntoASubstituteShader()
        {
            var spec = Entry("Material", "shared/materials/test/Fx.json");
            spec.kind = "material";
            spec.renderQueue = 2450;
            var shader = Shader.Find("UI/Default");
            var report = new SceneBuildReport();

            var material = MaterialFactory.Create(spec, shader, true, new Dictionary<string, Object>(), report);
            try
            {
                Assert.AreEqual(shader.renderQueue, material.renderQueue);
                Assert.IsTrue(report.materialPropertiesDropped.Any(entry => entry.Contains("render queue 2450")));
            }
            finally
            {
                Object.DestroyImmediate(material);
            }
        }

        [Test]
        public void TextureLargerThan2048KeepsItsSize()
        {
            var path = WritePng(Folder + "/Wide.png", 4096, 8);
            var spec = Entry("Texture2D", "shared/Wide.png");

            SceneAssetImporter.ConfigureImporter(spec, path, true, new SceneBuildReport());

            var importer = (TextureImporter)AssetImporter.GetAtPath(path);
            Assert.AreEqual(4096, importer.GetDefaultPlatformTextureSettings().maxTextureSize);
            Assert.AreEqual(4096, AssetDatabase.LoadAssetAtPath<Texture2D>(path).width);
        }

        [Test]
        public void SingleChannelOriginalImportsAsAlpha8()
        {
            var path = WritePng(Folder + "/Atlas.png", 64, 64);
            var spec = Entry("Texture2D", "shared/Atlas.png");
            spec.alphaOnly = true;

            SceneAssetImporter.ConfigureImporter(spec, path, true, new SceneBuildReport());

            Assert.AreEqual(TextureFormat.Alpha8, AssetDatabase.LoadAssetAtPath<Texture2D>(path).format);
        }

        [Test]
        public void FailedBuildRemovesTheFilesItCreated()
        {
            // A ".png" that is not an image is copied, then fails to load as a Sprite after the copy.
            Directory.CreateDirectory(Path.Combine(_sourceRoot, "shared"));
            File.WriteAllText(Path.Combine(_sourceRoot, "shared", "Broken.png"), "not an image");
            var spec = new SceneSpec
            {
                schema = "uare.scene-spec", version = 2, scene = "UareRollbackProbe", scopeId = "test", originalPath = "", source = "", serializedFile = "",
                sourceRoot = _sourceRoot, rootOrderBasis = "path_id", tags = Array.Empty<string>(), layers = Array.Empty<string>(),
                sortingLayers = SortingLayer.layers.Select(layer => new SortingLayerSpec { name = layer.name, uniqueId = (uint)layer.id }).ToArray(),
                gameObjects = Array.Empty<GameObjectSpec>(), sceneSettings = Array.Empty<SceneSettingSpec>(),
                assets = new[] { Entry("Sprite", "shared/Broken.png") },
            };
            var specPath = Path.Combine(_sourceRoot, "spec.json");
            File.WriteAllText(specPath, JsonUtility.ToJson(spec));
            LogAssert.ignoreFailingMessages = true; // Unity logs the failed image import.

            Assert.Throws<SceneBuildException>(() => UareSceneBuilder.BuildScene(specPath, Folder));

            Assert.IsFalse(File.Exists(Folder + "/Shared/Broken.png"), "the copied file was left behind");
            Assert.IsFalse(File.Exists(Folder + "/Scenes/UareRollbackProbe.unity"), "a scene was saved");
        }
    }
}
