"""A least-privilege registry role for client VMs (V2-837).

The daily production write test needs its uploader and verifier VMs to reach
the registry: the uploader claims pool files as it uploads them, the verifiers
read back the address list and record what they found. Handing those VMs the
operator's ``DATABASE_URL`` would give a public, internet-facing box full
read/write over every deployment, every result and every gate verdict in the
registry.

This module mints a role that can only touch the tables those two services
actually use, and only in the ways they use them:

* the pool catalogue is **read-only** — a VM may look at what is available but
  may never add, retire or re-price an entry;
* claims are read/write, because claiming and resolving is the whole job;
* the two result tables are read/write, because that is where the VMs record
  what happened;
* everything else in the registry is not granted at all.

Deliberately **not** granted: DDL of any kind, DELETE anywhere, and any access
to ``deployments`` beyond SELECT. A compromised upload VM can spend pool files
and write false results — which is unavoidable, since that is its job — but it
cannot destroy history, drop tables, or read unrelated deployment data.

The SQL is built here as pure functions so the grant set is unit-testable
without a database, and so reviewing what a VM can do means reading one list.
"""

import re
import secrets

#: Read-only: the catalogue itself. A VM chooses from it, never edits it.
READ_ONLY_TABLES = (
    "prod_file_pool",
    "prod_file_pool_digests",
    "prod_file_pool_tags",
    # Needed so the foreign keys on the tables below resolve, and so a service
    # can confirm the deployment it was told it belongs to actually exists.
    "deployments",
)

#: Read/write: claiming files and recording outcomes is the point of the role.
#: Note the absence of DELETE — a claim is corrected by resolving or releasing
#: it, never by erasing the fact that it happened.
READ_WRITE_TABLES = (
    "prod_file_pool_claims",
    "deployment_prod_upload_results",
    "deployment_prod_upload_verifications",
)

#: Sequences behind the read/write tables' surrogate keys. Without USAGE the
#: INSERTs fail at run time with a permission error that reads like a bug.
SEQUENCES = (
    "prod_file_pool_claims_id_seq",
    "deployment_prod_upload_results_id_seq",
    "deployment_prod_upload_verifications_id_seq",
)

_VALID_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PoolClientRoleError(Exception):
    pass


def validate_role_name(name: str) -> str:
    """Reject anything that would need quoting or could smuggle SQL.

    Role names cannot be parameterised in DDL, so they are concatenated into
    the statement — which makes validating them the only thing standing
    between a typo and an injection.
    """
    if not _VALID_ROLE_NAME.match(name or ""):
        raise PoolClientRoleError(
            f"invalid role name {name!r}: use lower-case letters, digits and "
            "underscores, starting with a letter or underscore (max 63 chars)"
        )
    return name


def generate_password(length: int = 40) -> str:
    """URL-safe so it can be dropped into a connection string unescaped."""
    return secrets.token_urlsafe(length)[:length]


def build_grant_statements(
    role: str, password: str = "", create_role: bool = True
) -> list[str]:
    """Statements that grant exactly the role's set, optionally creating it.

    ``create_role=False`` for a managed cluster (DigitalOcean, RDS…), where the
    provider owns user creation and the application user has no ``CREATEROLE``.
    The grants themselves still work there, because they are made by the owner
    of the tables rather than by a superuser.

    Idempotent: safe to re-run to rotate the password or to pick up a table
    added by a later migration.
    """
    validate_role_name(role)

    statements = []
    if create_role:
        if not password:
            raise PoolClientRoleError("a password is required to create a role")
        # Passwords are escaped rather than parameterised for the same reason
        # as the role name: this is DDL.
        escaped = password.replace("'", "''")
        # CREATE ROLE has no IF NOT EXISTS, so the existence check is inline.
        statements.append(
            f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{escaped}';
            ELSE
                ALTER ROLE {role} WITH LOGIN PASSWORD '{escaped}';
            END IF;
        END
        $$;
        """.strip()
        )
        # These two need ownership of the database and the schema. On a managed
        # cluster the database is owned by the provider's admin user, so they
        # are skipped there — and they are unnecessary anyway, since PostgreSQL
        # grants CONNECT and schema USAGE to PUBLIC by default. Only the table
        # grants below actually confer anything, and those work because the
        # tables are owned by the user that ran the migrations.
        statements.append(
            f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {role}',
                           current_database());
        END
        $$;
        """.strip()
        )
        statements.append(f"GRANT USAGE ON SCHEMA public TO {role};")

    for table in READ_ONLY_TABLES:
        statements.append(f"GRANT SELECT ON {table} TO {role};")
    for table in READ_WRITE_TABLES:
        statements.append(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role};")
    for sequence in SEQUENCES:
        statements.append(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {role};")
    return statements


def build_revoke_statements(role: str) -> list[str]:
    validate_role_name(role)
    return [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};",
        f"REVOKE ALL ON SCHEMA public FROM {role};",
        f"DROP ROLE IF EXISTS {role};",
    ]


def build_client_url(admin_url: str, role: str, password: str) -> str:
    """Rewrite an admin URL to use the scoped role's credentials.

    Host, port, database and query string (crucially ``sslmode=require``) are
    taken from the admin URL, so the client cannot end up connecting less
    securely than the operator does.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(admin_url)
    if not parts.hostname:
        raise PoolClientRoleError(f"cannot parse a host out of {admin_url!r}")
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
