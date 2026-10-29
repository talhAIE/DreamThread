"""Connection policy shared by the API, worker, migrations and Neon provisioning."""

import certifi
from sqlalchemy.engine import make_url


def is_neon(url):
    return bool(url.host and url.host.casefold().endswith(".neon.tech"))


def postgres_url(value, *, neon_required=False):
    """Accept copied PostgreSQL URLs; normalize to our psycopg driver without exposing secrets."""
    try:
        url = make_url(value)
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg")
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError
        if neon_required and not is_neon(url):
            raise ValueError
        if is_neon(url):
            if "-pooler" in url.host.casefold():
                raise ValueError
            if not url.username or not url.password:
                raise ValueError
            if url.query.get("sslmode") not in {None, "require", "verify-full"}:
                raise ValueError
            url = url.update_query_dict({"sslmode": "verify-full"})
        return url
    except Exception:
        raise ValueError(
            "Use a PostgreSQL psycopg URL; Neon requires credentials, a direct endpoint "
            "(pooling off), and verified TLS"
        ) from None


def connect_args(value, timeout=15):
    url = postgres_url(value)
    args = {"connect_timeout": int(timeout)}
    if is_neon(url) and "sslrootcert" not in url.query:
        # libpq on Windows does not automatically use Windows' certificate store.
        # Resolve the CA bundle in each runtime/container instead of storing a host file path.
        args["sslrootcert"] = certifi.where()
    return args
