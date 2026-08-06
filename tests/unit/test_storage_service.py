from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import FileTooLargeError, UnsupportedImageError
from app.services.storage_service import LocalStorageService, validate_image_upload


class TestValidation:
    @pytest.mark.parametrize("filename", ["photo.jpg", "photo.JPEG", "scan.png", "img.heic"])
    def test_extensions_acceptees(self, filename: str):
        assert validate_image_upload(filename, b"x" * 10) == Path(filename).suffix.lower()

    @pytest.mark.parametrize("filename", ["doc.pdf", "archive.zip", "sans_extension", None])
    def test_extensions_refusees(self, filename: str | None):
        with pytest.raises(UnsupportedImageError):
            validate_image_upload(filename, b"x" * 10)

    def test_fichier_vide_refuse(self):
        with pytest.raises(UnsupportedImageError):
            validate_image_upload("photo.jpg", b"")

    def test_fichier_trop_volumineux_refuse(self, monkeypatch: pytest.MonkeyPatch):
        from app.services import storage_service

        monkeypatch.setattr(
            type(storage_service.settings), "max_upload_size_bytes", property(lambda self: 10)
        )
        with pytest.raises(FileTooLargeError):
            validate_image_upload("photo.jpg", b"x" * 11)


class TestLocalStorage:
    async def test_sauvegarde_le_fichier_et_retourne_une_url_publique(self, tmp_path: Path):
        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/storage/uploads")

        url = await storage.save(b"contenu-binaire", folder="mrz", extension=".jpg")

        assert url.startswith("/storage/uploads/mrz/")
        assert url.endswith(".jpg")
        relative = url.removeprefix("/storage/uploads/")
        assert (tmp_path / relative).read_bytes() == b"contenu-binaire"

    async def test_les_fichiers_sont_ranges_par_annee_et_mois(self, tmp_path: Path):
        from datetime import UTC, datetime

        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/s")
        url = await storage.save(b"x", folder="signatures", extension=".png")

        now = datetime.now(UTC)
        assert f"/signatures/{now:%Y}/{now:%m}/" in url

    async def test_deux_sauvegardes_ne_se_marchent_pas_dessus(self, tmp_path: Path):
        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/s")

        premier = await storage.save(b"a", folder="mrz", extension=".jpg")
        second = await storage.save(b"b", folder="mrz", extension=".jpg")

        assert premier != second

    async def test_suppression_dun_fichier_existant(self, tmp_path: Path):
        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/s")
        url = await storage.save(b"x", folder="mrz", extension=".jpg")

        await storage.delete(url)

        assert not (tmp_path / url.removeprefix("/s/")).exists()

    async def test_suppression_dun_fichier_absent_ne_leve_pas(self, tmp_path: Path):
        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/s")
        await storage.delete("/s/mrz/2026/01/inexistant.jpg")

    async def test_le_path_traversal_est_neutralise(self, tmp_path: Path):
        cible = tmp_path.parent / "fichier_sensible.txt"
        cible.write_text("à ne pas supprimer", encoding="utf-8")
        storage = LocalStorageService(base_dir=str(tmp_path), public_base_url="/s")

        await storage.delete("/s/../fichier_sensible.txt")

        assert cible.exists()
        cible.unlink()
