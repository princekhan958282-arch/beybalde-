"""Repository asset photos resolve safely and update the existing builder."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from cogs.custom_bey import (
    ASSET_IMAGE_BASE, BuilderTextModal, CustomBeyBuilder, CustomBeyError,
    ImageChoiceView, asset_image_url,
)


class AssetPhotos(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "assets"
        self.root.mkdir()
        self.patch = patch("cogs.custom_bey.ASSETS_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / "beys").mkdir()
        photo = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
        photo.putpixel((0, 0), (0, 0, 0, 0))
        photo.save(self.root / "beys" / "My Bey.png")

    async def test_asset_paths_and_url_encoding(self):
        for value in ("assets/beys/My Bey.png", "beys/My Bey.png", "assets\\beys\\My Bey.png"):
            self.assertEqual(await asset_image_url(value), ASSET_IMAGE_BASE + "beys/My%20Bey.png")

    async def test_reject_missing_traversal_symlinks_and_invalid_images(self):
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes((self.root / "beys" / "My Bey.png").read_bytes())
        (self.root / "escape.png").symlink_to(outside)
        (self.root / "broken.png").write_bytes(b"not an image")
        Image.new("RGB", (10, 10)).save(self.root / "opaque.png")
        for value in ("../outside.png", str(outside), "escape.png", "missing.png", "broken.png", "opaque.png", "photo.jpg"):
            with self.subTest(value=value), self.assertRaises(CustomBeyError):
                await asset_image_url(value)

    async def test_asset_modal_keeps_url_schema_and_updates_builder(self):
        builder = CustomBeyBuilder(1, "Attack")
        builder._message = MagicMock()
        builder._message.edit = AsyncMock()
        modal = BuilderTextModal(builder, "image_asset")
        modal.value._value = "assets/beys/My Bey.png"
        interaction = MagicMock()
        interaction.user.id = 1
        interaction.response.defer = AsyncMock()
        with patch("cogs.custom_bey.validate_image_url", AsyncMock()) as validate:
            await modal.on_submit(interaction)
        expected = ASSET_IMAGE_BASE + "beys/My%20Bey.png"
        validate.assert_awaited_once_with(expected)
        self.assertEqual(builder.draft["image"], expected)
        builder._message.edit.assert_awaited_once()
        self.assertEqual([button.label for button in ImageChoiceView(builder).rows[0].children],
                         ["Image URL", "Assets Photo", "Upload Image"])


if __name__ == "__main__":
    unittest.main()
