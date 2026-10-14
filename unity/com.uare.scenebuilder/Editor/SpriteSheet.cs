using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using UnityEditor;
using UnityEditor.U2D.Sprites;
using UnityEngine;

namespace Uare.SceneBuilder.Editor
{
    // Reads and writes the sprite rects of a Multiple-mode image through the 2D Sprite package's data provider, so the
    // rects, pivots and borders the build cut from one texture become that texture's sprite sheet.
    internal static class SpriteSheet
    {
        public static void Write(TextureImporter importer, SpriteRectSpec[] sheet)
        {
            var provider = Provider(importer);
            var rects = sheet.Select(s => new SpriteRect
            {
                name = s.name,
                rect = new Rect(s.x, s.y, s.width, s.height),
                alignment = SpriteAlignment.Custom,
                pivot = new Vector2(s.pivotX, s.pivotY),
                border = new Vector4(s.borderLeft, s.borderBottom, s.borderRight, s.borderTop),
                spriteID = GUID.Generate(),
            }).ToArray();
            provider.SetSpriteRects(rects);
            var names = provider.GetDataProvider<ISpriteNameFileIdDataProvider>();
            if (names == null)
            {
                throw new SceneBuildException($"The sprite data provider cannot assign sprite names; path={importer.assetPath}");
            }

            names.SetNameFileIdPairs(rects.Select(r => new SpriteNameFileIdPair(r.name, r.spriteID)));
            provider.Apply();
        }

        // The sheet of an image already in the project, comparable with Describe(AssetSpec); empty unless Multiple.
        public static string Describe(TextureImporter importer)
        {
            if (importer.spriteImportMode != SpriteImportMode.Multiple)
            {
                return string.Empty;
            }

            return Format(Provider(importer).GetSpriteRects().Select(r => (r.name, r.rect, r.pivot, r.border)));
        }

        public static string Describe(AssetSpec asset)
        {
            if (asset.spriteMode != "multiple")
            {
                return string.Empty;
            }

            return Format(asset.sprites.Select(s => (s.name, new Rect(s.x, s.y, s.width, s.height), new Vector2(s.pivotX, s.pivotY),
                new Vector4(s.borderLeft, s.borderBottom, s.borderRight, s.borderTop))));
        }

        private static ISpriteEditorDataProvider Provider(TextureImporter importer)
        {
            var factory = new SpriteDataProviderFactories();
            factory.Init();
            var provider = factory.GetSpriteEditorDataProviderFromObject(importer);
            if (provider == null)
            {
                throw new SceneBuildException($"No sprite data provider for the image; path={importer.assetPath}");
            }

            provider.InitSpriteEditorDataProvider();
            return provider;
        }

        private static string Format(IEnumerable<(string Name, Rect Rect, Vector2 Pivot, Vector4 Border)> rects)
        {
            string Number(float value) => value.ToString("R", CultureInfo.InvariantCulture);
            return "|sheet:" + string.Join(";", rects.OrderBy(r => r.Name, System.StringComparer.Ordinal).Select(r => string.Join(",",
                r.Name, Number(r.Rect.x), Number(r.Rect.y), Number(r.Rect.width), Number(r.Rect.height), Number(r.Pivot.x), Number(r.Pivot.y),
                Number(r.Border.x), Number(r.Border.y), Number(r.Border.z), Number(r.Border.w))));
        }
    }
}
