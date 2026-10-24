import pytest
from pydantic import ValidationError

from app.core.config import Settings, describe_database_target, normalise_database_url
from app.middleware.rate_limit import FixedWindowLimiter


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_wildcard_cors_is_rejected():
    with pytest.raises(ValidationError):
        settings(cors_origins="*")
    with pytest.raises(ValidationError):
        settings(cors_origins="https://*.vercel.app")


def test_cors_origins_parse_from_comma_separated_env():
    s = settings(cors_origins="https://a.example.test, https://b.example.test/")
    assert s.cors_origins == ["https://a.example.test", "https://b.example.test"]


def test_production_forbids_demo_content_and_requires_database():
    with pytest.raises(ValidationError):
        settings(
            app_env="production", database_url="postgresql://u:p@db.example.test/postgres", expose_demo_content=True
        )
    with pytest.raises(ValidationError):
        settings(app_env="production")


@pytest.mark.parametrize(
    "raw",
    [
        "postgres://u:p@db.example.test:5432/postgres",
        "postgresql://u:p@db.example.test:5432/postgres",
        "postgresql+psycopg://u:p@db.example.test:5432/postgres",
    ],
)
def test_database_urls_are_normalised_to_psycopg_with_tls(raw):
    url = normalise_database_url(raw)
    assert url.drivername == "postgresql+psycopg"
    assert url.query["sslmode"] == "require"


def test_explicit_sslmode_and_local_hosts_are_respected():
    assert (
        normalise_database_url("postgresql://u@db.example.test/p?sslmode=verify-full").query["sslmode"] == "verify-full"
    )
    assert "sslmode" not in normalise_database_url("postgresql://u@localhost/p").query


@pytest.mark.parametrize("raw", ["mysql://u:p@h/db", "sqlite:///x.db", "postgresql+asyncpg://u@h/db", "not a url"])
def test_unsupported_database_urls_are_rejected(raw):
    with pytest.raises(ValidationError) as exc:
        settings(database_url=raw)
    assert "p@" not in str(exc.value)


def test_transaction_pooler_disables_prepared_statements():
    pooled = settings(database_url="postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    direct = settings(database_url="postgresql://u:p@db.abc.supabase.co:5432/postgres")
    assert pooled.use_prepared_statements() is False
    assert direct.use_prepared_statements() is True
    assert settings(
        database_url=pooled.database_url.get_secret_value(), db_prepared_statements=True
    ).use_prepared_statements()


def test_database_target_description_never_includes_credentials():
    s = settings(database_url="postgresql://user:hunter2@db.abc.supabase.co:5432/postgres")
    assert describe_database_target(s) == "db.abc.supabase.co:5432/postgres"
    assert "hunter2" not in repr(s)


def test_fixed_window_limiter_resets_after_window():
    clock = [0.0]
    limiter = FixedWindowLimiter(2, window_seconds=60, clock=lambda: clock[0])
    assert [limiter.hit("a")[0] for _ in range(3)] == [True, True, False]
    assert limiter.hit("b")[0] is True
    clock[0] = 61
    assert limiter.hit("a")[0] is True
