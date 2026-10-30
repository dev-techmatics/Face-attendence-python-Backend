"""Downloads the two ONNX models the self-hosted face recognition provider needs.

Two models are required, and they do different jobs:

1. **SCRFD** - finds faces and returns five landmarks (eyes, nose, mouth corners). The
   landmarks matter: without them a face cannot be aligned, and an unaligned crop costs far
   more accuracy than any threshold tuning recovers. A detector **with keypoints** is
   therefore mandatory.
2. **ArcFace** - turns an aligned 112x112 crop into a 512-float embedding.

Both come from the InsightFace model zoo, distributed as a ``buffalo`` bundle. The bundle also
contains landmark and gender/age models this service does not use; only the two needed files
are extracted, under stable names so one configuration works whichever pack you choose.

    buffalo_s   ~128 MB download   det_500m  + w600k_mbf   fast, good enough for a kiosk
    buffalo_l   ~288 MB download   det_10g   + w600k_r50   more accurate, slower per frame

The code is MIT; review the model card for the weights before shipping commercially. The
extracted files are large and must not be committed; .gitignore already excludes
``models/*.onnx``.

Usage::

    python scripts/download_models.py                 # buffalo_l
    python scripts/download_models.py --pack buffalo_s
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RELEASE_BASE = "https://github.com/deepinsight/insightface/releases/download/v0.7"

#: Stable names the application's default configuration points at. Keeping them independent
#: of the pack means switching packs is a re-run of this script, not a config edit - and the
#: provider id you set in ``model_version`` is what records which one is live.
DETECTOR_NAME = "scrfd_detector.onnx"
RECOGNIZER_NAME = "arcface_recognizer.onnx"

PACKS = {
    "buffalo_s": {
        "detector": "det_500m.onnx",
        "recognizer": "w600k_mbf.onnx",
        "note": "SCRFD 500M + MobileFaceNet, 512-d",
    },
    "buffalo_l": {
        "detector": "det_10g.onnx",
        "recognizer": "w600k_r50.onnx",
        "note": "SCRFD 10G + ResNet50, 512-d",
    },
}


def _progress(block_number: int, block_size: int, total_size: int) -> None:
    if total_size <= 0:
        return

    downloaded = min(block_number * block_size, total_size)
    percent = downloaded * 100 // total_size
    sys.stdout.write(f"\r         {percent:3d}%  {downloaded / 1_048_576:7.1f} MB")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack",
        choices=sorted(PACKS),
        default="buffalo_l",
        help="Which InsightFace bundle to take the two models from (default: buffalo_l).",
    )
    parser.add_argument(
        "--output-directory",
        default=str(PROJECT_ROOT / "models"),
        help="Where to put the .onnx files (default: ./models).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and overwrite models that are already present.",
    )
    arguments = parser.parse_args()

    pack = PACKS[arguments.pack]
    target = Path(arguments.output_directory).resolve()
    target.mkdir(parents=True, exist_ok=True)

    detector = target / DETECTOR_NAME
    recognizer = target / RECOGNIZER_NAME

    print(f"Pack:             {arguments.pack} ({pack['note']})")
    print(f"Target directory: {target}")

    if detector.is_file() and recognizer.is_file() and not arguments.force:
        print(
            f"  [skip] both models already present "
            f"({detector.stat().st_size / 1_048_576:.1f} MB + "
            f"{recognizer.stat().st_size / 1_048_576:.1f} MB). Pass --force to replace them."
        )
        return 0

    url = f"{RELEASE_BASE}/{arguments.pack}.zip"
    print(f"  [get ] {url}")

    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / f"{arguments.pack}.zip"

        try:
            urllib.request.urlretrieve(url, archive, _progress)
            sys.stdout.write("\n")
        except Exception as exc:
            sys.stdout.write("\n")
            print(f"  [fail] could not download {url}: {exc}")
            print(
                "\nSet the face provider to 'development' to work on the API without the "
                "models, or download the bundle by hand and extract "
                f"{pack['detector']} -> {DETECTOR_NAME} and "
                f"{pack['recognizer']} -> {RECOGNIZER_NAME}."
            )
            return 1

        try:
            with zipfile.ZipFile(archive) as bundle:
                members = {Path(name).name: name for name in bundle.namelist()}

                for wanted, destination in (
                    (pack["detector"], detector),
                    (pack["recognizer"], recognizer),
                ):
                    if wanted not in members:
                        print(
                            f"  [fail] {wanted} is not in {arguments.pack}.zip; it contains "
                            f"{sorted(members)}"
                        )
                        return 1

                    # Extract to a temporary name and move into place, so an interrupted run
                    # does not leave a truncated model that fails to load at startup.
                    partial = destination.with_suffix(destination.suffix + ".part")

                    with bundle.open(members[wanted]) as source, partial.open("wb") as sink:
                        shutil.copyfileobj(source, sink)

                    partial.replace(destination)
                    print(
                        f"  [ok  ] {wanted} -> {destination.name} "
                        f"({destination.stat().st_size / 1_048_576:.1f} MB)"
                    )
        except zipfile.BadZipFile as exc:
            print(f"  [fail] {archive} is not a readable zip: {exc}")
            return 1

    (target / "MODELS.txt").write_text(
        f"detector:   {pack['detector']} -> {DETECTOR_NAME}\n"
        f"recognizer: {pack['recognizer']} -> {RECOGNIZER_NAME}\n"
        f"pack:       {arguments.pack} ({pack['note']})\n"
        f"source:     {url}\n"
        "\n"
        "Swapping either file invalidates every stored template. Bump\n"
        "FaceRecognition:Onnx:ModelVersion when you do, so the provider id changes and the\n"
        "existing enrolments are marked Stale instead of being compared against vectors from\n"
        "a different model.\n",
        encoding="utf-8",
    )

    print("\nBoth models are in place. Set FA_FACE__PROVIDER=onnx to use them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
