from __future__ import annotations

import math

import numpy as np
import pytest

from app.core import vector_math


def vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float32)


def test_cosine_similarity_identical_vectors_is_one():
    v = vec(0.1, 0.5, -0.3, 0.8)
    assert vector_math.cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_opposite_vectors_is_minus_one():
    assert vector_math.cosine_similarity(vec(1, 0, 0), vec(-1, 0, 0)) == pytest.approx(
        -1.0, abs=1e-5
    )


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert vector_math.cosine_similarity(vec(1, 0), vec(0, 1)) == pytest.approx(0.0, abs=1e-5)


def test_cosine_similarity_ignores_magnitude():
    # Direction is what carries identity; a scaled copy must score the same.
    assert vector_math.cosine_similarity(vec(1, 2, 3), vec(10, 20, 30)) == pytest.approx(
        1.0, abs=1e-5
    )


def test_cosine_similarity_zero_vector_is_zero_not_nan():
    # A NaN here would propagate into a threshold comparison and quietly reject everybody.
    score = vector_math.cosine_similarity(vec(0, 0, 0), vec(1, 2, 3))

    assert not math.isnan(score)
    assert score == 0.0


def test_cosine_similarity_mismatched_lengths_raises():
    # Comparing templates from two different models is a bug, not a low score.
    with pytest.raises(ValueError, match="(?i)length mismatch"):
        vector_math.cosine_similarity(vec(1, 2), vec(1, 2, 3))


def test_to_bytes_from_bytes_round_trips_exactly():
    original = vec(0.123456, -0.987654, 0, 1, -1)
    restored = vector_math.from_bytes(vector_math.to_bytes(original))

    assert np.array_equal(original, restored)


def test_to_bytes_produces_4_bytes_per_dimension():
    # 512 dimensions must occupy exactly 2048 bytes, which is what the column is sized for.
    assert len(vector_math.to_bytes(np.zeros(512, dtype=np.float32))) == 2048


def test_from_bytes_partial_float_raises():
    with pytest.raises(ValueError):
        vector_math.from_bytes(bytes(7))


def test_l2_normalize_produces_unit_length():
    v = vector_math.l2_normalize(vec(3, 4))

    assert math.sqrt(float(v[0]) ** 2 + float(v[1]) ** 2) == pytest.approx(1.0, abs=1e-5)
    assert float(v[0]) == pytest.approx(0.6, abs=1e-4)
    assert float(v[1]) == pytest.approx(0.8, abs=1e-4)


def test_l2_normalize_zero_vector_leaves_it_alone():
    assert np.array_equal(vector_math.l2_normalize(vec(0, 0)), vec(0, 0))


def test_centroid_of_identical_vectors_is_that_vector():
    v = vec(0.6, 0.8)
    centroid = vector_math.centroid([v.copy(), v.copy(), v.copy()])

    assert float(centroid[0]) == pytest.approx(0.6, abs=1e-4)
    assert float(centroid[1]) == pytest.approx(0.8, abs=1e-4)


def test_centroid_is_unit_length():
    centroid = vector_math.centroid([vec(1, 0), vec(0, 1)])

    assert math.sqrt(
        float(centroid[0]) ** 2 + float(centroid[1]) ** 2
    ) == pytest.approx(1.0, abs=1e-5)


def test_centroid_mismatched_dimensions_raises():
    with pytest.raises(ValueError):
        vector_math.centroid([vec(1, 2), vec(1, 2, 3)])


def test_templates_are_little_endian_regardless_of_host():
    # Any other language reading the same column must get the same floats back.
    assert vector_math.to_bytes(vec(1.0)) == b"\x00\x00\x80?"
