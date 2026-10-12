using System.Collections.Generic;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor.Tests
{
    public sealed class SerializedPropertyWriterTests
    {
        private static readonly Dictionary<string, Object> NoObjects = new Dictionary<string, Object>();
        private static readonly Dictionary<string, Object> NoAssets = new Dictionary<string, Object>();

        private readonly List<Object> _created = new List<Object>();

        [TearDown]
        public void DestroyCreatedObjects()
        {
            foreach (var created in _created)
            {
                Object.DestroyImmediate(created);
            }

            _created.Clear();
        }

        private static PropertySpec Number(string path, double value)
        {
            return new PropertySpec { path = path, kind = "number", number = value };
        }

        private static WriteOutcome Apply(SerializedObject serialized, PropertySpec row, SceneBuildReport report,
            IReadOnlyDictionary<string, Object> objects)
        {
            var property = SerializedPropertyWriter.Find(serialized, row.path);
            Assert.IsNotNull(property, "property path not found: " + row.path);
            return SerializedPropertyWriter.Write(property, row, objects, NoAssets, report);
        }

        private PropertyFixture Fixture()
        {
            var fixture = ScriptableObject.CreateInstance<PropertyFixture>();
            _created.Add(fixture);
            return fixture;
        }

        [Test]
        public void ArraySizeRowResizesTheListBeforeItsElementsAreWritten()
        {
            var fixture = Fixture();
            var report = new SceneBuildReport();
            using (var serialized = new SerializedObject(fixture))
            {
                Assert.AreEqual(WriteOutcome.Applied,
                    Apply(serialized, new PropertySpec { path = "values.Array.size", kind = "arraySize", number = 3 }, report, NoObjects));
                for (var index = 0; index < 3; index++)
                {
                    Assert.AreEqual(WriteOutcome.Applied, Apply(serialized, Number($"values.Array.data[{index}]", 4 + index), report, NoObjects));
                }

                serialized.ApplyModifiedPropertiesWithoutUndo();
            }

            CollectionAssert.AreEqual(new[] { 4, 5, 6 }, fixture.values);
        }

        [Test]
        public void MaterialArraySizeOfABuiltInRendererIsWritten()
        {
            var gameObject = new GameObject("renderer");
            _created.Add(gameObject);
            // MeshRenderer keeps several material slots; SpriteRenderer clamps its array to one.
            var renderer = gameObject.AddComponent<MeshRenderer>();
            using (var serialized = new SerializedObject(renderer))
            {
                Assert.AreEqual(WriteOutcome.Applied,
                    Apply(serialized, new PropertySpec { path = "m_Materials.Array.size", kind = "arraySize", number = 2 }, new SceneBuildReport(), NoObjects));
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }

            Assert.AreEqual(2, renderer.sharedMaterials.Length);
        }

        [Test]
        public void EverythingLayerMaskAndLargeUnsignedValuesKeepTheirBits()
        {
            var fixture = Fixture();
            var report = new SceneBuildReport();
            using (var serialized = new SerializedObject(fixture))
            {
                Assert.AreEqual(WriteOutcome.Applied, Apply(serialized, Number("mask.m_Bits", 4294967295), report, NoObjects));
                Assert.AreEqual(WriteOutcome.Applied, Apply(serialized, Number("unsignedId", 4088999859), report, NoObjects));
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }

            Assert.AreEqual(-1, fixture.mask.value);
            Assert.AreEqual(4088999859u, fixture.unsignedId);
        }

        [Test]
        public void ReferenceOfTheWrongTypeIsRejectedAndReported()
        {
            var fixture = Fixture();
            var gameObject = new GameObject("target");
            _created.Add(gameObject);
            var report = new SceneBuildReport();
            var objects = new Dictionary<string, Object> { { "go", gameObject }, { "transform", gameObject.transform } };
            using (var serialized = new SerializedObject(fixture))
            {
                var wrong = new PropertySpec { path = "target", kind = "ref", refKind = "object", refTarget = "go", refClass = "GameObject" };
                Assert.AreEqual(WriteOutcome.NotWritten, Apply(serialized, wrong, report, objects));
                var right = new PropertySpec { path = "target", kind = "ref", refKind = "object", refTarget = "transform", refClass = "Transform" };
                Assert.AreEqual(WriteOutcome.Applied, Apply(serialized, right, report, objects));
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }

            Assert.AreEqual(gameObject.transform, fixture.target);
            Assert.AreEqual(1, report.referencesRejected);
            Assert.AreEqual(1, report.referencesToObjects);
        }

        [Test]
        public void UnknownReferenceKindFailsLoudly()
        {
            var fixture = Fixture();
            using (var serialized = new SerializedObject(fixture))
            {
                var row = new PropertySpec { path = "target", kind = "ref", refKind = "guess" };
                Assert.Throws<SceneBuildException>(() => Apply(serialized, row, new SceneBuildReport(), NoObjects));
            }
        }
    }
}
