"""Embedding helpers.

Templates are stored as float32 little-endian so the same bytes can be read back on any CPU
and by any other language talking to the same database.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

#: float32 little-endian, which is what every stored template uses.
_DTYPE = np.dtype("<f4")


def cosine_similarity(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Cosine similarity in ``[-1, 1]``.

    Inputs are expected to be L2-normalised already, in which case this is a plain dot
    product; it still normalises defensively so a caller passing raw vectors gets a correct
    answer rather than a silently inflated one.
    """
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()

    if a.size != b.size:
        raise ValueError(
            f"Embedding length mismatch: {a.size} vs {b.size}. This usually means two "
            "different models produced these templates."
        )

    if a.size == 0:
        return 0.0

    dot = float(np.dot(a.astype(np.float64), b.astype(np.float64)))
    norm_a = float(np.sqrt(np.sum(a.astype(np.float64) ** 2)))
    norm_b = float(np.sqrt(np.sum(b.astype(np.float64) ** 2)))

    if norm_a <= 1e-8 or norm_b <= 1e-8:
        return 0.0

    return float(min(1.0, max(-1.0, dot / (norm_a * norm_b))))


def l2_normalize(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    """Scale a vector to unit length. Returns the same array, modified in place."""
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.sqrt(np.sum(vector.astype(np.float64) ** 2)))

    if norm <= 1e-8:
        return vector

    vector /= np.float32(norm)
    return vector


def to_bytes(vector: NDArray[np.float32]) -> bytes:
    """Serialise an embedding to float32 little-endian bytes."""
    return np.asarray(vector, dtype=np.float32).astype(_DTYPE, copy=False).tobytes()


def from_bytes(data: bytes) -> NDArray[np.float32]:
    """Deserialise float32 little-endian bytes back into an embedding."""
    if len(data) % 4 != 0:
        raise ValueError(
            f"Template payload of {len(data)} bytes is not a whole number of float32 values."
        )

    return np.frombuffer(data, dtype=_DTYPE).astype(np.float32, copy=True)


def centroid(vectors: list[NDArray[np.float32]]) -> NDArray[np.float32]:
    """Element-wise mean of several embeddings, re-normalised.

    A per-person centroid like this is a cheap way to cut a 1:N search down to one comparison
    per person; it is a coarser signal than per-pose matching, so use it to shortlist, not to
    decide.
    """
    if not vectors:
        return np.empty(0, dtype=np.float32)

    dim = np.asarray(vectors[0]).size

    if any(np.asarray(v).size != dim for v in vectors):
        raise ValueError("All embeddings must share one dimension.")

    stacked = np.vstack([np.asarray(v, dtype=np.float32).ravel() for v in vectors])
    return l2_normalize(stacked.mean(axis=0, dtype=np.float32))
