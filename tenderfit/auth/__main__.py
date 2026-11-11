"""Operator commands. A password is read from a prompt, or TENDERFIT_PASSWORD for scripts; it is never a command-line argument.

  python -m tenderfit.auth create-user someone@example.org
  python -m tenderfit.auth grant someone@example.org "Supplier name"
  python -m tenderfit.auth list-unowned"""
import argparse
import getpass
import os
import sys

from tenderfit.auth import AuthError, create_user, grant
from tenderfit.db import connect, migrate


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m tenderfit.auth")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-user").add_argument("email")
    g = sub.add_parser("grant", help="give a person access to an existing supplier profile")
    g.add_argument("email")
    g.add_argument("profile")
    sub.add_parser("list-unowned", help="supplier profiles nobody can reach yet")
    a = p.parse_args()
    with connect() as conn:
        migrate(conn)
        if a.cmd == "create-user":
            password = os.environ.get("TENDERFIT_PASSWORD") or getpass.getpass("Password: ")
            try:
                print("created user", create_user(conn, a.email, password))
            except AuthError as e:
                sys.exit(str(e))
        elif a.cmd == "grant":
            user = conn.execute("SELECT id FROM app_user WHERE lower(email) = lower(%s)", (a.email,)).fetchone()
            profile = conn.execute("SELECT id FROM business_profile WHERE name = %s", (a.profile,)).fetchone()
            if not user or not profile:
                sys.exit("no such user or profile")
            grant(conn, user["id"], profile["id"])
            print("granted")
        else:
            for r in conn.execute("""SELECT p.id, p.name FROM business_profile p WHERE NOT EXISTS
                                     (SELECT 1 FROM profile_member m WHERE m.profile_id = p.id) ORDER BY p.id"""):
                print(r["id"], r["name"])
        conn.commit()


if __name__ == "__main__":
    main()
