#if UNITY_EDITOR
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Uare.SceneBuilder.PlayModeTests
{
    // Loads every scene under Assets additively in Play Mode, runs it for a fixed number of frames and fails on any
    // unhandled exception or logged error. Scenes do not need to be in Build Settings. Each result is also appended to
    // Logs/uare-playmode.jsonl with a run identifier, so the outcome does not depend on how a client tracks the run.
    // This is a load-and-run smoke test; it does not exercise gameplay or input.
    public sealed class ScenePlayModeTests
    {
        private const int FramesToRun = 120;
        private static readonly string RunId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ", CultureInfo.InvariantCulture);

        public static IEnumerable<string> ScenePaths()
        {
            // The test runner's own temporary scene is not a project scene.
            return AssetDatabase.FindAssets("t:Scene", new[] { "Assets" })
                .Select(AssetDatabase.GUIDToAssetPath)
                .Where(path => !Path.GetFileName(path).StartsWith("InitTestScene", StringComparison.Ordinal))
                .OrderBy(path => path, StringComparer.Ordinal)
                .ToArray();
        }

        [UnityTest]
        public IEnumerator SceneLoadsAndRunsWithoutExceptionsOrErrors([ValueSource(nameof(ScenePaths))] string scenePath)
        {
            var problems = new List<string>();
            var loaded = false;
            var rootObjects = -1;

            void Receive(string condition, string stackTrace, LogType type)
            {
                if (type == LogType.Exception || type == LogType.Error || type == LogType.Assert)
                {
                    problems.Add(type + ": " + condition);
                }
            }

            // Collect logged errors and report them all in one assertion instead of stopping at the first one.
            LogAssert.ignoreFailingMessages = true;
            Application.logMessageReceived += Receive;
            try
            {
                var load = EditorSceneManager.LoadSceneAsyncInPlayMode(scenePath, new LoadSceneParameters(LoadSceneMode.Additive));
                Assert.IsNotNull(load, "scene load did not start: " + scenePath);
                yield return load;
                var scene = SceneManager.GetSceneByPath(scenePath);
                loaded = scene.IsValid() && scene.isLoaded;
                if (loaded)
                {
                    SceneManager.SetActiveScene(scene);
                    for (var frame = 0; frame < FramesToRun; frame++)
                    {
                        yield return null;
                    }

                    loaded = scene.isLoaded;
                    rootObjects = scene.rootCount;
                    yield return SceneManager.UnloadSceneAsync(scene);
                }
            }
            finally
            {
                Application.logMessageReceived -= Receive;
                LogAssert.ignoreFailingMessages = false;
                Record(scenePath, loaded, rootObjects, problems);
            }

            Assert.IsTrue(loaded, "scene did not load: " + scenePath);
            Assert.IsEmpty(problems, scenePath + " logged: " + string.Join(" | ", problems));
        }

        private static void Record(string scenePath, bool loaded, int rootObjects, List<string> problems)
        {
            var line = new StringBuilder();
            line.Append("{\"runId\":").Append(Quote(RunId));
            line.Append(",\"scene\":").Append(Quote(scenePath));
            line.Append(",\"loaded\":").Append(loaded ? "true" : "false");
            line.Append(",\"rootObjects\":").Append(rootObjects.ToString(CultureInfo.InvariantCulture));
            line.Append(",\"problems\":[").Append(string.Join(",", problems.Select(Quote))).Append("]}\n");
            var file = Path.Combine(Path.GetDirectoryName(Application.dataPath), "Logs", "uare-playmode.jsonl");
            Directory.CreateDirectory(Path.GetDirectoryName(file));
            File.AppendAllText(file, line.ToString());
        }

        private static string Quote(string text)
        {
            var builder = new StringBuilder(text.Length + 2).Append('"');
            foreach (var character in text)
            {
                if (character == '"' || character == '\\')
                {
                    builder.Append('\\').Append(character);
                }
                else if (character < 0x20 || character == (char)0x85 || character == (char)0x2028 || character == (char)0x2029)
                {
                    builder.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                }
                else
                {
                    builder.Append(character);
                }
            }

            return builder.Append('"').ToString();
        }
    }
}
#endif
