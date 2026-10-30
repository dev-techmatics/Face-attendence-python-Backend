"""Decode, letterbox, warp and measure. The pixel plumbing around the two models.

Images are carried as ``(height, width, 3)`` uint8 RGB numpy arrays.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFile, UnidentifiedImageError

from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceBox

#: Refuse to expand a decompression bomb into memory. 64 megapixels is far beyond any camera
#: frame this service has a use for, and Pillow raises rather than allocating.
Image.MAX_IMAGE_PIXELS = 64_000_000

#: A frame that lost its last few bytes in transit still decodes; recognition on a slightly
#: short JPEG beats refusing a punch outright.
ImageFile.LOAD_TRUNCATED_IMAGES = True

#: Luma weights, matching the original implementation.
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float64)


@dataclass(frozen=True)
class Letterboxed:
    """A letterboxed square plus what it takes to map coordinates back."""

    pixels: NDArray[np.uint8]
    size: int
    scale: float
    pad_x: float
    pad_y: float


def decode(content: bytes) -> Image.Image:
    """Decode an upload, or raise a corrupt-image error."""
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise FaceProcessingException(
            FaceError.CorruptImage,
            "The image could not be decoded. It may be truncated or not an image at all.",
            inner=exc,
        ) from exc

    # EXIF orientation is honoured here so a portrait phone capture is not analysed sideways.
    try:
        from PIL import ImageOps as PilImageOps

        image = PilImageOps.exif_transpose(image) or image
    except Exception:  # pragma: no cover - a broken EXIF block must not fail the upload
        pass

    return image.convert("RGB")


def to_array(image: Image.Image) -> NDArray[np.uint8]:
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def letterbox(source: Image.Image, size: int) -> Letterboxed:
    """Resize into a square canvas preserving aspect ratio, padding the remainder.

    Squashing to a square instead would distort faces and cost real accuracy.
    """
    scale = min(size / source.width, size / source.height)
    scaled_width = int(round(source.width * scale))
    scaled_height = int(round(source.height * scale))
    pad_x = (size - scaled_width) / 2.0
    pad_y = (size - scaled_height) / 2.0

    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    resized = source.resize((max(1, scaled_width), max(1, scaled_height)), Image.BILINEAR)
    canvas.paste(resized, (int(pad_x), int(pad_y)))

    return Letterboxed(to_array(canvas), size, scale, pad_x, pad_y)


def warp_aligned(
    source: Image.Image, transform: tuple[float, float, float, float, float, float], size: int
) -> NDArray[np.uint8]:
    """Warp a face into the canonical square the recognition model was trained on.

    Uses the five landmarks. Alignment is not an optimisation; an unaligned crop costs far
    more accuracy than any threshold tuning can recover.

    ``transform`` is the forward (source -> destination) similarity transform as
    ``(a, b, tx, c, d, ty)``; Pillow wants the inverse, which is computed here.
    """
    a, b, tx, c, d, ty = transform
    determinant = (a * d) - (b * c)

    if abs(determinant) < 1e-12:
        raise FaceProcessingException(
            FaceError.LowQuality,
            "The alignment transform is degenerate; the face is probably too small or occluded.",
        )

    # Inverse of [[a, b, tx], [c, d, ty]], which is what Image.transform(AFFINE) expects: it
    # maps each destination pixel back to a source coordinate.
    inv_a = d / determinant
    inv_b = -b / determinant
    inv_c = -c / determinant
    inv_d = a / determinant
    inv_tx = -((inv_a * tx) + (inv_b * ty))
    inv_ty = -((inv_c * tx) + (inv_d * ty))

    warped = source.transform(
        (size, size),
        Image.AFFINE,
        (inv_a, inv_b, inv_tx, inv_c, inv_d, inv_ty),
        resample=Image.BILINEAR,
        fillcolor=(0, 0, 0),
    )

    return to_array(warped)


def crop(source: Image.Image, box: FaceBox, size: int) -> NDArray[np.uint8]:
    """Crop the box and scale it to a square, on a black background."""
    canvas = Image.new("RGB", (size, size), (0, 0, 0))

    left = int(round(box.x))
    top = int(round(box.y))
    right = max(left + 1, int(round(box.right)))
    bottom = max(top + 1, int(round(box.bottom)))

    region = source.crop((left, top, right, bottom)).resize((size, size), Image.BILINEAR)
    canvas.paste(region, (0, 0))

    return to_array(canvas)


def mean_brightness(image: NDArray[np.uint8]) -> float:
    """Mean luma in 0..1. Flags frames that are blown out or lost in shadow."""
    if image.size == 0:
        return 0.0

    return float((image.astype(np.float64) @ _LUMA).mean() / 255.0)


def laplacian_variance(image: NDArray[np.uint8]) -> float:
    """Variance of the Laplacian - the standard cheap blur detector.

    A sharp face has strong second derivatives; motion blur flattens them.
    """
    height, width = image.shape[0], image.shape[1]

    if width < 3 or height < 3:
        return 0.0

    gray = (image.astype(np.float32) @ _LUMA.astype(np.float32)).astype(np.float32)

    values = (
        (4.0 * gray[1:-1, 1:-1])
        - gray[1:-1, :-2]
        - gray[1:-1, 2:]
        - gray[:-2, 1:-1]
        - gray[2:, 1:-1]
    ).astype(np.float64)

    if values.size == 0:
        return 0.0

    mean = float(values.mean())
    mean_squares = float((values * values).mean())

    return max(0.0, mean_squares - (mean * mean))
