using System.Collections.Generic;
using UnityEngine;

namespace Uare.SceneBuilder.Editor.Tests
{
    // Serialized field shapes that the type tree flattens differently from the editor's property paths.
    public sealed class PropertyFixture : ScriptableObject
    {
        public List<int> values = new List<int>();
        public LayerMask mask;
        public uint unsignedId;
        public Transform target;
    }
}
