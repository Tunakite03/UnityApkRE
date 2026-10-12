using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;

namespace Uare.SceneBuilder.Editor
{
    // Il2CppDumper stub assemblies reference Il2CppDummyDll. Their types declare the original fields but carry none of
    // the original logic, so bindings to them are reported apart from bindings to real code.
    internal static class StubAssemblies
    {
        private static readonly Dictionary<Assembly, bool> Known = new Dictionary<Assembly, bool>();

        public static bool Contains(Type type)
        {
            if (!Known.TryGetValue(type.Assembly, out var stub))
            {
                stub = type.Assembly.GetReferencedAssemblies().Any(reference => reference.Name == "Il2CppDummyDll");
                Known[type.Assembly] = stub;
            }

            return stub;
        }
    }
}
