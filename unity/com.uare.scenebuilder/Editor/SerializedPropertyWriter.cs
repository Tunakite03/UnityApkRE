using System;
using System.Collections.Generic;
using UnityEditor;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor
{
    internal enum WriteOutcome
    {
        Applied,
        TypeMismatch,
        NotWritten,
    }

    // Applies a list of spec rows to one serialized object; the outcome goes to `counts`, references to the report.
    internal static class PropertyApplier
    {
        public static void Apply(SerializedObject serialized, PropertySpec[] rows, IReadOnlyDictionary<string, Object> objects,
            IReadOnlyDictionary<string, Object> assets, SceneBuildReport report, PropertyCounts counts, string label)
        {
            foreach (var row in rows)
            {
                var property = SerializedPropertyWriter.Find(serialized, row.path);
                if (property == null)
                {
                    counts.unmatched++;
                    SceneBuildReport.Sample(counts.unmatchedSamples, label + ":" + row.path);
                    continue;
                }

                switch (SerializedPropertyWriter.Write(property, row, objects, assets, report))
                {
                    case WriteOutcome.Applied:
                        counts.applied++;
                        break;
                    case WriteOutcome.TypeMismatch:
                        counts.typeMismatch++;
                        SceneBuildReport.Sample(counts.mismatchSamples, $"{label}:{row.path} ({row.kind}->{property.propertyType})");
                        break;
                    case WriteOutcome.NotWritten:
                        counts.notWritten++;
                        break;
                }
            }
        }
    }

    // Writes one flattened spec row into a SerializedProperty. Rows use Unity property paths, so array sizes arrive as
    // "x.Array.size" and LayerMasks as "x.m_Bits".
    internal static class SerializedPropertyWriter
    {
        public static SerializedProperty Find(SerializedObject serialized, string path)
        {
            var property = serialized.FindProperty(path);
            if (property != null || !path.EndsWith(".m_Bits", StringComparison.Ordinal))
            {
                return property;
            }

            // A LayerMask may be one property in the editor but a struct with m_Bits in the type tree.
            var mask = serialized.FindProperty(path.Substring(0, path.Length - ".m_Bits".Length));
            return mask != null && mask.propertyType == SerializedPropertyType.LayerMask ? mask : null;
        }

        public static WriteOutcome Write(SerializedProperty property, PropertySpec row, IReadOnlyDictionary<string, Object> objects,
            IReadOnlyDictionary<string, Object> assets, SceneBuildReport report)
        {
            switch (row.kind)
            {
                case "arraySize":
                    // "x.Array.size" resolves to the size property itself; a bare array path resolves to the array.
                    if (property.propertyType == SerializedPropertyType.ArraySize)
                    {
                        property.intValue = (int)row.number;
                        return WriteOutcome.Applied;
                    }

                    if (!property.isArray)
                    {
                        return WriteOutcome.TypeMismatch;
                    }

                    property.arraySize = (int)row.number;
                    return WriteOutcome.Applied;
                case "string":
                    if (property.propertyType != SerializedPropertyType.String)
                    {
                        return WriteOutcome.TypeMismatch;
                    }

                    property.stringValue = row.text;
                    return WriteOutcome.Applied;
                case "number":
                {
                    var value = NumberValue(row);
                    if ((double.IsNaN(value) || double.IsInfinity(value)) && property.propertyType != SerializedPropertyType.Float)
                    {
                        return WriteOutcome.TypeMismatch;
                    }

                    return WriteNumber(property, value) ? WriteOutcome.Applied : WriteOutcome.TypeMismatch;
                }
                case "ref":
                    return WriteReference(property, row, objects, assets, report);
                default:
                    throw new SceneBuildException($"Unknown property kind in scene spec; kind={row.kind}, path={row.path}");
            }
        }

        // NaN/Infinity cannot be JSON numbers, so the spec carries their spelling in `text` (validated by the reader).
        internal static readonly IReadOnlyDictionary<string, double> NonFiniteNumbers = new Dictionary<string, double>
        {
            { "NaN", double.NaN }, { "Infinity", double.PositiveInfinity }, { "-Infinity", double.NegativeInfinity },
        };

        private static double NumberValue(PropertySpec row)
        {
            return string.IsNullOrEmpty(row.text) ? row.number : NonFiniteNumbers[row.text];
        }

        private static bool WriteNumber(SerializedProperty property, double value)
        {
            switch (property.propertyType)
            {
                case SerializedPropertyType.Integer:
                    if (property.numericType == SerializedPropertyNumericType.UInt64)
                    {
                        property.ulongValue = (ulong)value;
                    }
                    else
                    {
                        property.longValue = (long)value;
                    }

                    return true;
                case SerializedPropertyType.Boolean:
                    property.boolValue = value != 0;
                    return true;
                case SerializedPropertyType.Float:
                    property.doubleValue = value;
                    return true;
                case SerializedPropertyType.Enum:
                case SerializedPropertyType.Character:
                case SerializedPropertyType.ArraySize:
                    property.intValue = (int)(long)value;
                    return true;
                case SerializedPropertyType.LayerMask:
                    property.intValue = unchecked((int)(uint)(long)value);
                    return true;
                default:
                    return false;
            }
        }

        private static WriteOutcome WriteReference(SerializedProperty property, PropertySpec row, IReadOnlyDictionary<string, Object> objects,
            IReadOnlyDictionary<string, Object> assets, SceneBuildReport report)
        {
            if (property.propertyType != SerializedPropertyType.ObjectReference)
            {
                return WriteOutcome.TypeMismatch;
            }

            Object value;
            switch (row.refKind)
            {
                case "null":
                    property.objectReferenceValue = null;
                    report.referencesNull++;
                    return WriteOutcome.Applied;
                case "missing":
                    SceneBuildReport.Increment(report.referencesMissing, row.refClass);
                    return WriteOutcome.NotWritten;
                case "unresolved":
                    report.referencesUnresolved++;
                    return WriteOutcome.NotWritten;
                case "object":
                    if (!objects.TryGetValue(row.refTarget, out value))
                    {
                        // The target is a component this project could not create; it is listed in unsupportedComponents.
                        report.referencesToDroppedComponents++;
                        return WriteOutcome.NotWritten;
                    }

                    break;
                case "asset":
                    if (!assets.TryGetValue(row.refTarget, out value))
                    {
                        throw new SceneBuildException($"Referenced asset was not imported; asset={row.refTarget}, path={row.path}");
                    }

                    if (ReferenceEquals(value, null))
                    {
                        // A ScriptableObject whose script is not in this project; listed in scriptablesWithoutScript.
                        SceneBuildReport.Increment(report.referencesMissing, row.refClass + " (script not in project)");
                        return WriteOutcome.NotWritten;
                    }

                    if (value == null)
                    {
                        throw new SceneBuildException($"Referenced asset object was destroyed before it was assigned; asset={row.refTarget}, path={row.path}");
                    }

                    break;
                default:
                    throw new SceneBuildException($"Unknown reference kind in scene spec; refKind={row.refKind}, path={row.path}");
            }

            property.objectReferenceValue = value;
            if (property.objectReferenceValue != value)
            {
                // Unity rejects a value whose type does not fit the field (for example a GameObject in a Transform field).
                report.referencesRejected++;
                SceneBuildReport.Sample(report.rejectedSamples, $"{row.path} <- {row.refClass}");
                return WriteOutcome.NotWritten;
            }

            if (row.refKind == "object")
            {
                report.referencesToObjects++;
            }
            else
            {
                report.referencesToAssets++;
            }

            return WriteOutcome.Applied;
        }
    }
}
