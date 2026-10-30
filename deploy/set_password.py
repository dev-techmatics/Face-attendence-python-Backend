"""Changes the dashboard password.

The password is typed daily, so it should be something a person can actually type. This
writes whatever you choose into the environment file and leaves everything else alone.

    python deploy\\set_password.py              ask for a password
    python deploy\\set_password.py --suggest    offer a short memorable one

Restart the service afterwards (deploy\\stop-server.cmd then deploy\\start-server.cmd).
Everyone signed in stays signed in; only new sign-ins use the new password.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.production"
KEYS = ROOT / "deploy" / "API-KEYS.txt"

#: Short, concrete, easy to say down a corridor and easy to type on a phone.
WORDS = (
    "blue green amber coral ivory olive rust slate teal plum "
    "fox owl wren lark hare deer pine oak fern reed "
    "north south east west dawn dusk tide moss"
).split()

#: Below this, a lockout is doing all the work and the password almost none.
MINIMUM = 8


def suggestion() -> str:
    return (
        f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(90) + 10}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suggest", action="store_true", help="propose a short memorable password"
    )
    arguments = parser.parse_args()

    if not ENV.is_file():
        print(f"  {ENV} is missing. Nothing changed.")
        return 1

    if arguments.suggest:
        proposed = suggestion()
        print(f"\n  Suggested: {proposed}\n")
        answer = input("  Use it? [y/N] ").strip().lower()

        if answer != "y":
            print("  Nothing changed.")
            return 1

        password = proposed
    else:
        print("\n  New dashboard password. It is typed daily, so keep it typeable.")
        print(f"  At least {MINIMUM} characters.\n")

        password = getpass("  Password: ")
        again = getpass("  Again:    ")

        if password != again:
            print("\n  Those did not match. Nothing changed.")
            return 1

    password = password.strip()

    if len(password) < MINIMUM:
        print(f"\n  Too short - at least {MINIMUM} characters. Nothing changed.")
        return 1

    if "\n" in password or "\r" in password:
        print("\n  A password cannot contain a line break. Nothing changed.")
        return 1

    text = ENV.read_text(encoding="utf-8")

    if re.search(r"^FA_ADMIN_UI_PASSWORD=.*$", text, flags=re.MULTILINE):
        text = re.sub(
            r"^FA_ADMIN_UI_PASSWORD=.*$",
            f"FA_ADMIN_UI_PASSWORD={password}",
            text,
            flags=re.MULTILINE,
        )
    else:
        text += f"\nFA_ADMIN_UI_PASSWORD={password}\n"

    ENV.write_text(text, encoding="utf-8")

    # Keep the operator's copy in step, or the file people actually read goes stale.
    if KEYS.is_file():
        keys = KEYS.read_text(encoding="utf-8")
        keys = re.sub(
            r"(DASHBOARD \(HR view[^\n]*\):\n)  .*",
            lambda m: m.group(1) + "  " + password,
            keys,
        )
        KEYS.write_text(keys, encoding="utf-8")

    print("\n  Changed. Now restart the service:")
    print("     deploy\\stop-server.cmd")
    print("     deploy\\start-server.cmd")
    print(f"\n  The new password is also recorded in {KEYS}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
