"""Finds people registered twice under different employee IDs.

Scores every stored face against every other with the same cosine similarity the matcher
uses, and reports any pair belonging to two different IDs that scores high enough to be the
same person. Read-only - it changes nothing.

    python deploy\find_duplicate_faces.py

Worth running after a spell of hand-registering, and after restoring from a backup. The
service refuses a duplicate at registration, archived records included, so a pair showing up
here either predates that check or is two genuinely similar faces - look at both photos
before deleting anything.
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os  # noqa: E402

os.environ["FA_ENV_FILE"] = ".env.production"
os.chdir(pathlib.Path(__file__).resolve().parents[1])

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.vector_math import cosine_similarity, from_bytes  # noqa: E402
from app.db.session import get_session_factory, init_engine  # noqa: E402
from app.domain.models import FaceTemplate, Person  # noqa: E402


async def main() -> None:
    settings = get_settings()
    init_engine(settings)

    face = settings.face
    print("  thresholds in force")
    print(f"    clock in, 1:1 (device says who you are)   {face.verification_threshold}")
    print(f"    clock in, 1:N (search everybody)          {face.identification_threshold}")
    print(f"    refuse a duplicate registration           {face.duplicate_enrollment_threshold}")
    print(f"    duplicate check switched on               {face.reject_duplicate_enrollments}")
    print()

    factory = get_session_factory()

    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    Person.external_id,
                    Person.full_name,
                    FaceTemplate.id,
                    FaceTemplate.pose,
                    FaceTemplate.template_data,
                )
                .join(Person, Person.id == FaceTemplate.person_id)
                # Archived people included on purpose: their face is still on file, and
                # a second identity for somebody who merely left is exactly the thing to find.
                .where(FaceTemplate.is_active == True)  # noqa: E712
                .order_by(Person.external_id)
            )
        ).all()

    print(f"  {len(rows)} active face templates\n")

    best: dict[tuple[str, str], float] = {}

    for i, (id_a, name_a, _, _, data_a) in enumerate(rows):
        for id_b, name_b, _, _, data_b in rows[i + 1 :]:
            if id_a == id_b:
                continue

            score = cosine_similarity(from_bytes(data_a), from_bytes(data_b))
            key = (f"{name_a} ({id_a})", f"{name_b} ({id_b})")
            if score > best.get(key, float("-inf")):
                best[key] = score

    print("  best score between each pair of DIFFERENT people:\n")

    for (a, b), score in sorted(best.items(), key=lambda kv: kv[1], reverse=True):
        if score >= face.duplicate_enrollment_threshold:
            verdict = "REFUSED as duplicate"
        elif score >= face.verification_threshold:
            verdict = "<-- can clock in as each other, but was NOT refused"
        else:
            verdict = "different people"

        print(f"    {score:5.3f}   {a:28} vs {b:28} {verdict}")


asyncio.run(main())
