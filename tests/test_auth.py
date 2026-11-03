"""The shared auth surface, driven end to end against a real app.

WHY A REAL APP AND NOT MOCKS
    Everything that has actually broken in this code was an INTEGRATION
    detail — a cookie the browser silently discarded, a refresh cookie scoped
    to the wrong path so logout could not revoke it, a token that stayed valid
    after logout. None of that is visible when the pieces are mocked, so these
    drive a FastAPI app with real fastapi-jwt-auth and a mongomock `users`
    collection, and assert on the cookies and status codes that come back.
"""

from __future__ import annotations

import mongomock
import pytest
from fastapi import Depends, FastAPI
from fastapi_jwt_auth import AuthJWT
from httpx import ASGITransport, AsyncClient

from apex_common.auth import AuthConfig, install
from apex_common.auth.denylist import Denylist
from apex_common.auth.passwords import hash_password, verify_password

USER = {"name": "Ada", "email": "ada@example.com", "password": "hunter2hunter2",
        "password_confirm": "hunter2hunter2", "role": "user", "is_active": True}


@pytest.fixture
def realm():
    """One service's auth realm: its own cookie prefix, secret, and collections.

    AuthJWT config is PROCESS-GLOBAL, so each test reinstalls — which is also
    what proves install() is callable more than once in a process.
    """
    db = mongomock.MongoClient()["db"]
    cfg = {"value": AuthConfig(
        jwt_secret_key="test-secret", cookie_prefix="task_",
        access_token_expires_in=60, refresh_token_expires_in=43200,
    )}
    bundle = install(
        auth_jwt_cls=AuthJWT,
        config_provider=lambda: cfg["value"],
        users=lambda: db["users"],
        denylist_collection=lambda: db["revoked_tokens"],
    )
    app = FastAPI()
    app.include_router(bundle.router)

    @app.get("/whoami")
    def whoami(user_id: str = Depends(bundle.get_current_user)):
        return {"user_id": user_id}

    @app.get("/admin-only")
    def admin_only(user_id: str = Depends(bundle.require_admin)):
        return {"user_id": user_id}

    return app, db, bundle, cfg


@pytest.fixture
def client(realm):
    app, *_ = realm
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ===========================================================================
# Passwords
# ===========================================================================

def test_a_hash_is_not_the_password():
    h = hash_password("hunter2hunter2")
    assert h != "hunter2hunter2" and verify_password("hunter2hunter2", h)


def test_the_wrong_password_does_not_verify():
    assert not verify_password("nope", hash_password("hunter2hunter2"))


def test_two_hashes_of_one_password_differ():
    """bcrypt salts, so an identical password must not produce an identical row
    — otherwise the hashes themselves leak which users share a password."""
    assert hash_password("same-password") != hash_password("same-password")


# ===========================================================================
# Register
# ===========================================================================

async def test_register_creates_a_user_and_never_returns_the_password(client, realm):
    _, db, *_ = realm
    r = await client.post("/auth/register", json=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success" and body["email"] == "ada@example.com"
    assert "password" not in body
    stored = db["users"].find_one({"email": "ada@example.com"})
    assert stored["password"] != USER["password"], "the password was stored in clear"


async def test_the_email_is_stored_lowercased(client, realm):
    _, db, *_ = realm
    await client.post("/auth/register", json={**USER, "email": "Ada@Example.COM"})
    assert db["users"].find_one({"email": "ada@example.com"})


async def test_a_duplicate_email_is_refused(client):
    await client.post("/auth/register", json=USER)
    r = await client.post("/auth/register", json=USER)
    assert r.status_code == 400 and "already registered" in r.json()["detail"].lower()


async def test_mismatched_passwords_are_refused(client):
    r = await client.post("/auth/register",
                          json={**USER, "password_confirm": "something-else"})
    assert r.status_code == 400 and "match" in r.json()["detail"].lower()


# ===========================================================================
# Login — and the cookies, which is where the bugs were
# ===========================================================================

async def test_login_returns_the_access_token_but_never_the_refresh_token(client):
    await client.post("/auth/register", json=USER)
    r = await client.post("/auth/login",
                          json={"email": USER["email"], "password": USER["password"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body.get("refresh_token") is None, (
        "the refresh token must never reach JavaScript — returning it is what "
        "put a 30-day credential in localStorage"
    )


async def test_the_refresh_cookie_is_httponly_and_scoped_to_auth(client):
    """Two separate bugs pinned at once: a readable refresh cookie, and one
    scoped to "/" which sent a 30-day credential on every API call."""
    await client.post("/auth/register", json=USER)
    r = await client.post("/auth/login",
                          json={"email": USER["email"], "password": USER["password"]})
    raw = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
    joined = "\n".join(r.headers.get_list("set-cookie"))
    refresh = [l for l in joined.splitlines() if l.startswith("task_refresh_token=")]
    assert refresh, joined
    assert "HttpOnly" in refresh[0]
    assert "Path=/auth" in refresh[0], refresh[0]


async def test_the_cookies_are_namespaced_by_prefix(client):
    """Cookies ignore the PORT, so on localhost two services are one jar and
    unprefixed names clobber each other."""
    await client.post("/auth/register", json=USER)
    r = await client.post("/auth/login",
                          json={"email": USER["email"], "password": USER["password"]})
    names = {c.split("=")[0] for c in r.headers.get_list("set-cookie")}
    assert names == {"task_access_token", "task_refresh_token", "task_logged_in"}


async def test_logged_in_is_readable_because_it_carries_no_authority(client):
    await client.post("/auth/register", json=USER)
    r = await client.post("/auth/login",
                          json={"email": USER["email"], "password": USER["password"]})
    flag = [l for l in r.headers.get_list("set-cookie") if l.startswith("task_logged_in=")]
    assert flag and "HttpOnly" not in flag[0], (
        "logged_in must be readable — it lets the shell render signed-in before "
        "the first refresh"
    )


async def test_bad_credentials_say_nothing_about_which_half_was_wrong(client):
    await client.post("/auth/register", json=USER)
    wrong_pw = await client.post("/auth/login",
                                 json={"email": USER["email"], "password": "wrongwrongwrong"})
    no_user = await client.post("/auth/login",
                                json={"email": "nobody@example.com", "password": "wrongwrongwrong"})
    assert wrong_pw.status_code == no_user.status_code == 400
    assert wrong_pw.json()["detail"] == no_user.json()["detail"] == "Invalid credentials"


# ===========================================================================
# The token actually authenticates
# ===========================================================================

async def test_a_bearer_token_authenticates(client):
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    r = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["user_id"]


async def test_no_token_is_a_401_that_says_so(client):
    r = await client.get("/whoami")
    assert r.status_code == 401 and r.json()["detail"] == "You are not logged in"


async def test_a_garbage_token_is_a_401(client):
    r = await client.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_a_token_for_a_deleted_user_is_a_401(client, realm):
    """The user document is read on EVERY request, so deleting a user takes
    effect immediately rather than when their token expires."""
    _, db, *_ = realm
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    db["users"].delete_many({})
    r = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["detail"] == "User no longer exists"


async def test_a_deactivated_user_is_a_401(client, realm):
    _, db, *_ = realm
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    db["users"].update_many({}, {"$set": {"is_active": False}})
    r = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["detail"] == "Please verify your account"


# ===========================================================================
# Admin
# ===========================================================================

async def test_a_normal_user_is_not_an_admin(client):
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    r = await client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_the_admin_role_is_read_from_mongo_not_the_token(client, realm):
    """Granted AFTER the token was minted, and it takes effect at once — which
    is the whole reason the role is not a JWT claim."""
    _, db, *_ = realm
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    db["users"].update_many({}, {"$set": {"role": "admin"}})
    r = await client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_revoking_admin_takes_effect_immediately(client, realm):
    _, db, *_ = realm
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    db["users"].update_many({}, {"$set": {"role": "admin"}})
    assert (await client.get("/admin-only",
            headers={"Authorization": f"Bearer {token}"})).status_code == 200
    db["users"].update_many({}, {"$set": {"role": "user"}})
    assert (await client.get("/admin-only",
            headers={"Authorization": f"Bearer {token}"})).status_code == 403


# ===========================================================================
# Logout actually revokes
# ===========================================================================

async def test_logout_revokes_the_access_token(client, realm):
    """Clearing cookies alone left the token usable for its full life — this is
    the bug the denylist exists for."""
    _, db, *_ = realm
    await client.post("/auth/register", json=USER)
    token = (await client.post("/auth/login", json={
        "email": USER["email"], "password": USER["password"]})).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/whoami", headers=auth)).status_code == 200
    assert (await client.get("/auth/logout", headers=auth)).status_code == 200
    assert db["revoked_tokens"].count_documents({}) >= 1
    r = await client.get("/whoami", headers=auth)
    assert r.status_code == 401, "the token still works after logout"


async def test_logout_works_without_a_token(client):
    """It must: refusing would leave a user with a broken token stuck signed in."""
    r = await client.get("/auth/logout")
    assert r.status_code == 200 and r.json()["status"] == "success"


async def test_logout_deletes_the_refresh_cookie_on_its_own_path(client):
    """A delete at "/" alone leaves the browser holding the /auth-scoped cookie.

    TWO deletions go out and both are needed: fastapi-jwt-auth's
    `unset_jwt_cookies` clears at "/", and the explicit one clears at "/auth",
    which is where the refresh cookie actually lives. Only the second one
    retires the 30-day credential.
    """
    r = await client.get("/auth/logout")
    deletes = [l for l in r.headers.get_list("set-cookie")
               if l.startswith("task_refresh_token=")]
    paths = {l.split("Path=")[1].split(";")[0] for l in deletes}
    assert "/auth" in paths, deletes


# ===========================================================================
# Refresh
# ===========================================================================

async def test_refresh_mints_a_new_access_token_from_the_cookie(client):
    await client.post("/auth/register", json=USER)
    await client.post("/auth/login",
                      json={"email": USER["email"], "password": USER["password"]})
    r = await client.get("/auth/refresh")       # the cookie jar carries it
    assert r.status_code == 200 and r.json()["access_token"]


async def test_refresh_without_a_token_says_which_token_is_missing(client):
    r = await client.get("/auth/refresh")
    assert r.status_code == 401
    assert r.json()["detail"] == "Please provide refresh token"


async def test_refresh_stops_working_after_logout(client):
    """The refresh token is the 30-day credential; revoking it is the point."""
    await client.post("/auth/register", json=USER)
    await client.post("/auth/login",
                      json={"email": USER["email"], "password": USER["password"]})
    assert (await client.get("/auth/refresh")).status_code == 200
    await client.get("/auth/logout")
    assert (await client.get("/auth/refresh")).status_code == 401


# ===========================================================================
# The cookie policy guard
# ===========================================================================

def test_samesite_none_without_secure_is_refused():
    """Browsers discard that combination silently, so every request would
    arrive unauthenticated with nothing logging a reason."""
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        AuthConfig(cookie_samesite="none", cookie_secure=False).check_cookie_policy()


@pytest.mark.parametrize("samesite,secure", [
    ("none", True), ("lax", False), ("strict", False), ("None", True), (" NONE ", True),
])
def test_acceptable_cookie_policies_pass(samesite, secure):
    AuthConfig(cookie_samesite=samesite, cookie_secure=secure).check_cookie_policy()


def test_the_guard_fires_at_install_because_that_is_startup():
    """fastapi-jwt-auth reads its config at DECORATION time and never again, so
    a bad cookie policy must fail here — at the service's boot — rather than on
    some later request. See config.py, "WHEN THE PROVIDER IS READ"."""
    db = mongomock.MongoClient()["db"]
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        install(
            auth_jwt_cls=AuthJWT,
            config_provider=lambda: AuthConfig(
                jwt_secret_key="s", cookie_samesite="none", cookie_secure=False),
            users=lambda: db["users"],
            denylist_collection=lambda: db["revoked_tokens"],
        )


async def test_the_router_reads_cookie_config_per_request(client, realm):
    """This is where the callable genuinely earns its keep: the ROUTER's own
    cookie attributes are read per request, so a change after install is
    honoured — unlike the library's own settings, which are frozen at install."""
    _, _, _, cfg = realm
    await client.post("/auth/register", json=USER)
    cfg["value"] = AuthConfig(jwt_secret_key="test-secret", cookie_prefix="renamed_",
                              cookie_samesite="none", cookie_secure=True)
    r = await client.post("/auth/login",
                          json={"email": USER["email"], "password": USER["password"]})
    names = {c.split("=")[0] for c in r.headers.get_list("set-cookie")}
    assert names == {"renamed_access_token", "renamed_refresh_token", "renamed_logged_in"}
    assert all("Secure" in c for c in r.headers.get_list("set-cookie"))


# ===========================================================================
# The denylist fails OPEN — including when it was never wired up
# ===========================================================================

def test_an_unreachable_denylist_reports_not_revoked():
    def boom():
        raise ConnectionError("mongo is down")
    assert Denylist(boom, default_ttl_minutes=60).is_revoked("some-jti") is False


def test_a_denylist_with_no_collection_reports_not_revoked():
    """The never-configured case reaches the same fail-open path on purpose: a
    boot-order mistake must not reject every request in the service."""
    assert Denylist(lambda: None, default_ttl_minutes=60).is_revoked("jti") is False


def test_a_revoke_against_no_collection_is_false_not_an_exception():
    """Logout must still succeed from the user's point of view."""
    assert Denylist(lambda: None, default_ttl_minutes=60).revoke("jti", None) is False


def test_ensure_indexes_against_no_collection_does_not_raise():
    Denylist(lambda: None, default_ttl_minutes=60).ensure_indexes()


def test_a_revoked_jti_is_reported_revoked():
    db = mongomock.MongoClient()["db"]
    dl = Denylist(lambda: db["revoked_tokens"], default_ttl_minutes=60)
    assert dl.revoke("jti-1", None) is True
    assert dl.is_revoked("jti-1") is True
    assert dl.is_revoked("jti-2") is False


def test_revoking_twice_records_one_row():
    db = mongomock.MongoClient()["db"]
    dl = Denylist(lambda: db["revoked_tokens"], default_ttl_minutes=60)
    dl.revoke("jti-1", None)
    dl.revoke("jti-1", None)
    assert db["revoked_tokens"].count_documents({"_id": "jti-1"}) == 1


def test_a_missing_expiry_falls_back_to_the_longest_token_life():
    """A row with no expiry would never retire and the collection would grow
    without bound."""
    db = mongomock.MongoClient()["db"]
    Denylist(lambda: db["revoked_tokens"], default_ttl_minutes=43200).revoke("j", None)
    row = db["revoked_tokens"].find_one({"_id": "j"})
    assert row["expires_at"] is not None


def test_a_naive_expiry_is_treated_as_utc():
    """A naive datetime would otherwise be read in the server's local zone, so
    the TTL could retire a row hours early or late.

    Asserted on what the code SENDS, not on what comes back: Mongo (and
    mongomock) store datetimes as naive UTC, so a round trip cannot show it.
    """
    from datetime import datetime, timezone

    sent = {}

    class _Coll:
        def update_one(self, flt, update, upsert=False):
            sent.update(update["$set"])

    Denylist(lambda: _Coll(), default_ttl_minutes=60).revoke(
        "j", datetime(2030, 1, 1, 12, 0, 0)
    )
    assert sent["expires_at"] == datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_a_blank_jti_is_never_stored():
    db = mongomock.MongoClient()["db"]
    dl = Denylist(lambda: db["revoked_tokens"], default_ttl_minutes=60)
    assert dl.revoke(None, None) is False and dl.revoke("", None) is False
    assert db["revoked_tokens"].count_documents({}) == 0


# ===========================================================================
# Two realms in one process do not share cookies
# ===========================================================================

async def test_a_second_realm_uses_its_own_cookie_names(realm):
    """The localhost collision, as a test: two services, one cookie jar, and
    unprefixed names clobber each other."""
    db = mongomock.MongoClient()["db"]
    bundle = install(
        auth_jwt_cls=AuthJWT,
        config_provider=lambda: AuthConfig(jwt_secret_key="other-secret",
                                           cookie_prefix="ing_"),
        users=lambda: db["users"],
        denylist_collection=lambda: db["revoked_tokens"],
    )
    app = FastAPI()
    app.include_router(bundle.router)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        await c.post("/auth/register", json=USER)
        r = await c.post("/auth/login",
                         json={"email": USER["email"], "password": USER["password"]})
        names = {x.split("=")[0] for x in r.headers.get_list("set-cookie")}
    assert names == {"ing_access_token", "ing_refresh_token", "ing_logged_in"}
