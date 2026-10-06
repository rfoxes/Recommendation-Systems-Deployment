"""The CTR model's files: a release of the public model repo, pinned by version and SHA-256.

The model is trained offline (the training data never leaves the model repo) and exported with
`python -m src.export` in https://github.com/rfoxes/Recommendation-Systems. This module downloads the
pinned release and verifies every file, so the Docker build and local runs get byte-identical files.
Run directly to prefetch: python -m app.ranking.model_files
"""

import hashlib
import logging
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL_REPO = "rfoxes/Recommendation-Systems"
MODEL_RELEASE = "v1.0.0"
RELEASE_URL = f"https://github.com/{MODEL_REPO}/releases/download/{MODEL_RELEASE}"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / f"ctr-{MODEL_RELEASE}"

FILES = {
    "lightgbm.txt": "42759f0dce2a32ef96a2a01a4e11d9d99094679b508bb521c6f0bca2994c9aab",
    "fm_weights.npz": "008c016b682017ba49640b05cf6a6419258de5cbab8438bc247f6aa416430911",
    "encoders.json": "6634697ce049403f330f367f822a06ddae16a5296365649b767d07640f985cad",
    "golden_sample.json": "e5995976e7339543b3043de5657e4d0e263326c62683df9d82795118912db389",
    "manifest.json": "3d56823e46b77bf7c418adcbd3c9b7ee4d569db2697f9a7f229bf3c90a8fb334",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_model_files(directory: Path = DEFAULT_MODEL_DIR) -> Path:
    """Download any missing or mismatched file from the pinned release; refuse files with the wrong checksum."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        path = directory / name
        if path.exists() and _sha256(path) == expected:
            continue
        logger.info("Downloading %s from %s %s", name, MODEL_REPO, MODEL_RELEASE)
        with (
            tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp,
            urllib.request.urlopen(f"{RELEASE_URL}/{name}", timeout=60) as response,  # pinned https URL
        ):
            tmp.write(response.read())
        downloaded = Path(tmp.name)
        if _sha256(downloaded) != expected:
            downloaded.unlink()
            raise RuntimeError(f"{name} from {MODEL_RELEASE} failed its checksum; refusing to use it")
        downloaded.chmod(0o644)  # temp files are owner-only; the app may run as a different user
        downloaded.replace(path)
    return directory


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"CTR model files ready in {ensure_model_files()}")
