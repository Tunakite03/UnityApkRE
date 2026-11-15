using System;
using System.Collections.Generic;
using UnityEditor;
using Object = UnityEngine.Object;

namespace Uare.SceneBuilder.Editor
{
    // Unity's own resources by class name and object name: the runtime set ("Library/unity default resources") and the
    // editor's built-in extras ("Resources/unity_builtin_extra"). A spec reference of kind "builtin" is resolved here,
    // so a scene points at the same resource the original project used instead of a copied file.
    internal static class BuiltinResources
    {
        private static readonly string[] Files = { "Library/unity default resources", "Resources/unity_builtin_extra" };
        private static Dictionary<string, Object> _byKey;

        public static Object Find(string className, string name)
        {
            if (_byKey == null)
            {
                _byKey = new Dictionary<string, Object>(StringComparer.Ordinal);
                foreach (var file in Files)
                {
                    foreach (var resource in AssetDatabase.LoadAllAssetsAtPath(file))
                    {
                        if (resource == null)
                        {
                            continue;
                        }

                        var key = resource.GetType().Name + "/" + resource.name;
                        if (!_byKey.ContainsKey(key))
                        {
                            _byKey.Add(key, resource);
                        }
                    }
                }
            }

            return _byKey.TryGetValue(className + "/" + name, out var found) ? found : null;
        }
    }
}
