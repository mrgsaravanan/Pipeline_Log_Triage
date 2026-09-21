"""Create the triage tables, 3 teams and 2 users per team in the local Postgres.

    export DATABASE_URL="postgresql://postgres:<password>@localhost:5432/triage"
    python seed_db.py

Safe to re-run: existing tables, teams and users are left alone. Passwords are
random and are printed ONCE, for users created by this run - only a salted hash
is stored, so they cannot be recovered later.
"""

import sys

import triage_db


def main() -> int:
    if not triage_db.db_configured():
        print(f"set {triage_db.DB_ENV} first, e.g. "
              "postgresql://postgres:<password>@localhost:5432/triage", file=sys.stderr)
        return 1

    with triage_db.connect() as conn:
        triage_db.apply_schema(conn)
        created = triage_db.seed(conn)

    if not created:
        print("Teams and users already exist - nothing new created.")
        return 0

    print(f"{'team':<28} {'username':<16} {'role':<9} password")
    for team, username, role, password in created:
        print(f"{team:<28} {username:<16} {role:<9} {password}")
    print("\nSave these now - they are not stored anywhere in readable form.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
