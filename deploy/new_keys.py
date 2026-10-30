"""Issues fresh API keys for the kiosk app.

The keys get typed into a phone's settings screen, read down a corridor and pasted into
config files, so they avoid everything that makes that go wrong: no punctuation at the start
(a leading dash is eaten by shells and by anything that parses flags), no characters that
look like each other (0/O, 1/l/I), and groups of eight so a person can keep their place.

    python deploy\\new_keys.py            replace both keys
    python deploy\\new_keys.py --device   replace only the kiosk key
    python deploy\\new_keys.py --admin    replace only the admin key

The old key stops working the moment the service restarts, so change the kiosks over in the
same sitting. Restart afterwards:

    deploy\\stop-server.cmd
    deploy\\start-server.cmd
"""

from __future__ import annotations

import argparse
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.production"
KEYS = ROOT / "deploy" / "API-KEYS.txt"

#: Letters and digits with the look-alikes removed. 54 characters, so each one carries about
#: 5.7 bits and a 32-character key carries far more than anything will ever brute-force.
ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

GROUPS = 4
GROUP_LENGTH = 8


def generate(prefix: str) -> str:
    """``fa-kiosk-abcdefgh-...`` - starts with a letter, reads out loud without ambiguity."""
    body = "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(GROUP_LENGTH))
        for _ in range(GROUPS)
    )
    return f"fa-{prefix}-{body}"


def replace_env_line(text: str, variable: str, key: str) -> str:
    """The variable holds a JSON list, because both lists accept several keys at once."""
    line = f'{variable}=["{key}"]'
    pattern = rf"^{re.escape(variable)}=.*$"

    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, line, text, flags=re.MULTILINE)

    return text + f"\n{line}\n"


def replace_keys_file_entry(text: str, heading: str, key: str) -> str:
    """Keep the operator's copy in step, or the file people read disagrees with the server."""
    pattern = rf"({re.escape(heading)}[^\n]*:\n)  .*"
    return re.sub(pattern, lambda m: m.group(1) + "  " + key, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", action="store_true", help="only the kiosk key")
    parser.add_argument("--admin", action="store_true", help="only the admin key")
    arguments = parser.parse_args()

    # Neither flag means both, which is what rotating after a leak needs.
    both = not arguments.device and not arguments.admin

    if not ENV.is_file():
        print(f"  {ENV} is missing. Nothing changed.")
        return 1

    text = ENV.read_text(encoding="utf-8")
    keys = KEYS.read_text(encoding="utf-8") if KEYS.is_file() else None
    changed: list[str] = []

    if both or arguments.device:
        device = generate("kiosk")
        text = replace_env_line(text, "FA_API_KEYS__DEVICE", device)
        if keys is not None:
            keys = replace_keys_file_entry(keys, "DEVICE", device)
        changed.append("DEVICE (the one that goes on a kiosk)")

    if both or arguments.admin:
        admin = generate("admin")
        text = replace_env_line(text, "FA_API_KEYS__ADMIN", admin)
        if keys is not None:
            keys = replace_keys_file_entry(keys, "ADMIN", admin)
        changed.append("ADMIN (full access)")

    ENV.write_text(text, encoding="utf-8")

    if keys is not None:
        KEYS.write_text(keys, encoding="utf-8")

    print("\n  New key issued for:")
    for entry in changed:
        print(f"    - {entry}")

    print("\n  Written to .env.production. Now restart the service:")
    print("     deploy\\stop-server.cmd")
    print("     deploy\\start-server.cmd")
    print(f"\n  Read the new value with deploy\\show-kiosk-key.cmd, or from {KEYS}.")
    print("  The previous key stops working at that restart.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
