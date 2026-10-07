import csv

import numpy as np
from PIL import Image, ImageOps
import onnxruntime as ort
from huggingface_hub import hf_hub_download, try_to_load_from_cache

MODEL_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
MODEL_REVISION = "v1.0"  # pinned release tag, as the model's author recommends
MODEL_FILES = ("model.onnx", "selected_tags.csv")
INPUT_SIZE = 448
# Frames tagged in a GIF or other animated image. Each one takes as long as
# tagging a still image.
ANIMATION_FRAMES = 3


def _cached_path(filename: str) -> str | None:
    cached = try_to_load_from_cache(MODEL_REPO, filename, revision=MODEL_REVISION)
    return cached if isinstance(cached, str) else None


def model_is_cached() -> bool:
    return all(_cached_path(f) for f in MODEL_FILES)


class Tagger:
    """The tagging model: downloaded on first use, then loaded from the cache."""

    def __init__(self):
        model_path, tags_path = (
            _cached_path(f) or hf_hub_download(MODEL_REPO, f, revision=MODEL_REVISION)
            for f in MODEL_FILES
        )
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        with open(tags_path, newline="", encoding="utf-8") as f:
            self._tag_names = [row["name"] for row in csv.DictReader(f)]

    def tag(self, image_path: str) -> dict[str, float]:
        """How sure the model is, from 0 to 1, that each tag it knows fits an image.

        For an animated image, each tag gets its highest score in any frame
        looked at, so an expression that only shows partway through counts.
        """
        scores = np.max(
            [
                self._session.run(None, {self._input_name: _preprocess(frame)})[0][0]
                for frame in _frames(image_path)
            ],
            axis=0,
        )
        return dict(zip(self._tag_names, scores.tolist()))


def _frames(image_path: str) -> list[Image.Image]:
    """The image, or for an animated one, ANIMATION_FRAMES frames spread across it."""
    with Image.open(image_path) as img:
        n = getattr(img, "n_frames", 1)
        # The middle of each equal stretch of the animation. In order, since
        # seeking back in a GIF decodes it again from the start.
        picks = sorted(
            {(2 * i + 1) * n // (2 * ANIMATION_FRAMES) for i in range(ANIMATION_FRAMES)}
        )
        frames = []
        for i in picks:
            img.seek(i)
            # Turn sideways camera photos upright, as the thumbnails do.
            frames.append(ImageOps.exif_transpose(img).convert("RGBA"))
    return frames


def _preprocess(img: Image.Image) -> np.ndarray:
    # Flatten transparency and pad to a square, both onto white: the model was
    # trained on white backgrounds, and a plain RGB conversion turns
    # transparent areas black.
    size = max(img.size)
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    canvas.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    padded = canvas.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)

    # The model expects a batch of float32 BGR images with values 0-255.
    return np.array(padded, dtype=np.float32)[np.newaxis, :, :, ::-1]
