"""Administrator bootstrap and account maintenance.

**There is no default production password, and no way to ask for one.** With no
configuration the bootstrap generates a 24-character random password, prints it
once, and marks the account `must_change_password`. A seeded credential that
ships with the software is the single most reliable way to lose a control plane.

**A password can be configured as a hash instead of plaintext.** Set
`TUTORTWIN_ADMIN_BOOTSTRAP_EMAIL` and `TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD_HASH`
in `.env`, then run `bootstrap` with no arguments. The file holds an Argon2id PHC
string, which is not a usable credential: a `.env` is read by every process on
the machine and leaks into shell history, backups and screen shares, so what
lives there should be worthless to whoever reads it. `hash-password` produces the
string.

Usage:

    python -m tutortwin.cli.admin hash-password       # prompts, prints a hash
    python -m tutortwin.cli.admin bootstrap           # reads .env
    python -m tutortwin.cli.admin bootstrap --email ops@example.com
    python -m tutortwin.cli.admin reset-password --email ops@example.com
    python -m tutortwin.cli.admin unlock --email ops@example.com
    python -m tutortwin.cli.admin list
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from tutortwin.config import get_settings
from tutortwin.db.admin_models import AdminUser
from tutortwin.db.engine import dispose_engine, init_engine, session_scope
from tutortwin.domain.admin import AdminRole
from tutortwin.repositories import admin as admin_repo
from tutortwin.runtime import configure_event_loop_policy
from tutortwin.security import admin_auth
from tutortwin.security.passwords import WeakPassword, generate_password, hash_password

PASSWORD_ENV = "TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD"  # noqa: S105 - an env var name
"""Optional plaintext override, for an automated deploy that injects a secret at
run time. Prefer the hash setting: this one puts a real password in the
environment, where the hash cannot be abused."""


async def _bootstrap(
    email: str | None, display_name: str, role: AdminRole, *, force_change: bool
) -> int:
    settings = get_settings()
    target_email = email or settings.admin_bootstrap_email
    if not target_email:
        print(
            "No email given. Pass --email, or set TUTORTWIN_ADMIN_BOOTSTRAP_EMAIL in .env.",
            file=sys.stderr,
        )
        return 2

    async with session_scope() as session:
        existing = await admin_repo.count_admins(session)
        if existing:
            # Refusing is the whole safety property: a bootstrap that also works
            # on a live system is a back door with a friendly name.
            print(
                f"Refusing: {existing} administrator account(s) already exist.\n"
                "Use reset-password, or create the account from the control plane.",
                file=sys.stderr,
            )
            return 2

        configured_hash = settings.admin_bootstrap_password_hash
        password: str | None = None
        password_hash: str | None = None

        if configured_hash is not None:
            # The hash comes straight from configuration; the plaintext never
            # exists in this process.
            password_hash = configured_hash.get_secret_value()
        else:
            password = os.environ.get(PASSWORD_ENV) or generate_password()

        # `create_admin` validates the address the same way the login endpoint
        # does, so a bootstrap cannot mint an account that can never sign in.
        user = await admin_repo.create_admin(
            session,
            email=target_email,
            password=password,
            password_hash=password_hash,
            role=role,
            display_name=display_name,
            must_change_password=force_change,
        )

        print("Administrator created.")
        print(f"  email : {user.email}")
        print(f"  role  : {user.role}")
        if password_hash is not None:
            print("  password: from TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD_HASH (not shown)")
        elif PASSWORD_ENV not in os.environ:
            print(f"  password (shown once): {password}")

        if force_change:
            print("\nThis password must be changed at first login.")
        return 0


def _hash_password_command(password: str | None) -> int:
    """Print an Argon2id hash for a password, for pasting into `.env`.

    Read from a prompt by default rather than taken as an argument: a password on
    the command line is captured by shell history and visible to anything reading
    the process table.
    """
    value = password or getpass.getpass("Password: ")
    if not value:
        print("No password entered.", file=sys.stderr)
        return 2
    try:
        digest = hash_password(value)
    except WeakPassword as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 2

    print(digest)
    return 0


async def _reset(email: str) -> int:
    async with session_scope() as session:
        user = (
            await session.execute(select(AdminUser).where(AdminUser.email == email.lower()))
        ).scalar_one_or_none()
        if user is None:
            print(f"No administrator with email {email}.", file=sys.stderr)
            return 2

        password = os.environ.get(PASSWORD_ENV) or generate_password()
        user.password_hash = hash_password(password)
        user.must_change_password = True
        user.failed_attempts = 0
        user.locked_until = None
        revoked = await admin_auth.revoke_all_for_user(
            session, admin_user_id=str(user.id), reason="cli_password_reset"
        )

        print(f"Password reset for {user.email}. {revoked} session(s) revoked.")
        if PASSWORD_ENV not in os.environ:
            print(f"  password (shown once): {password}")
        return 0


async def _unlock(email: str) -> int:
    async with session_scope() as session:
        user = (
            await session.execute(select(AdminUser).where(AdminUser.email == email.lower()))
        ).scalar_one_or_none()
        if user is None:
            print(f"No administrator with email {email}.", file=sys.stderr)
            return 2
        user.failed_attempts = 0
        user.locked_until = None
        print(f"Unlocked {user.email}.")
        return 0


async def _list() -> int:
    async with session_scope() as session:
        rows = await admin_repo.list_admins(session)
        if not rows:
            print("No administrators. Run `bootstrap` first.")
            return 0
        width = max(len(r.email) for r in rows)
        for row in rows:
            flag = " (must change password)" if row.must_change_password else ""
            print(f"{row.email:<{width}}  {row.role:<14} {row.status}{flag}")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tutortwin-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="Create the first administrator.")
    boot.add_argument("--email", default=None, help="Defaults to the value in .env.")
    boot.add_argument("--display-name", default="")
    boot.add_argument(
        "--role",
        default=AdminRole.SUPER_ADMIN.value,
        choices=[r.value for r in AdminRole],
    )
    boot.add_argument(
        "--no-force-change",
        action="store_true",
        help=(
            "Do not require a password change at first login. Only sensible when the "
            "password came from a hash you set deliberately."
        ),
    )

    hasher = sub.add_parser("hash-password", help="Print an Argon2id hash for pasting into .env.")
    hasher.add_argument(
        "--password",
        default=None,
        help="Read from a prompt when omitted, which keeps it out of shell history.",
    )

    reset = sub.add_parser("reset-password", help="Reset a password and revoke sessions.")
    reset.add_argument("--email", required=True)

    unlock = sub.add_parser("unlock", help="Clear a lockout after failed logins.")
    unlock.add_argument("--email", required=True)

    sub.add_parser("list", help="List administrators.")

    args = parser.parse_args(argv)

    # Hashing needs no database, so it runs before the engine is built. It is the
    # one command that is useful before anything else is configured.
    if args.command == "hash-password":
        return _hash_password_command(args.password)

    # psycopg cannot drive async IO on Windows's default ProactorEventLoop.
    # The API sets this at import; a CLI entry point has to set it too.
    configure_event_loop_policy()
    init_engine(get_settings())

    async def run() -> int:
        try:
            if args.command == "bootstrap":
                return await _bootstrap(
                    args.email,
                    args.display_name,
                    AdminRole(args.role),
                    force_change=not args.no_force_change,
                )
            if args.command == "reset-password":
                return await _reset(args.email)
            if args.command == "unlock":
                return await _unlock(args.email)
            return await _list()
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
