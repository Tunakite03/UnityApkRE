using UnityEngine;

namespace Uare.SceneBuilder
{
    // Stands in for a MonoBehaviour whose script is not in this project. It has no behavior. It keeps the original
    // script identity and the original serialized fields (type-tree JSON) so the real script can be bound later.
    // An empty ScriptClass means the original build itself had no script on this component.
    [AddComponentMenu("")]
    public sealed class UareMissingScript : MonoBehaviour
    {
        [SerializeField] private string scriptClass = string.Empty;
        [SerializeField] private string scriptNamespace = string.Empty;
        [SerializeField] private string scriptAssembly = string.Empty;
        [SerializeField] private string sourceObjectId = string.Empty;
        [SerializeField, TextArea(3, 12)] private string serializedJson = string.Empty;

        public string ScriptClass => scriptClass;
        public string ScriptNamespace => scriptNamespace;
        public string ScriptAssembly => scriptAssembly;
        public string SourceObjectId => sourceObjectId;
        public string SerializedJson => serializedJson;
    }
}
