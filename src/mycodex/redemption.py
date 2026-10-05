"""Earned-reset redemption through an isolated, account-specific official app-server."""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterator

from . import appserver, auth, config, paths, profiles, quota, resolve, state, ui, validation as check


class AccountServer(appserver.StdioAppServer):
    def __init__(self, home: Path, profile: profiles.Profile):
        super().__init__(home, timeout=30)
        self.profile = profile
        self.creds: auth.Credentials | None = None

    def open(self) -> None:
        self.creds = auth.credentials(self.profile.home)
        if not self.creds.account_id:
            raise appserver.AppServerError("the profile has no ChatGPT account id; reauthenticate it")
        super().open()
        self.result("account/login/start", {"type": "chatgptAuthTokens", "accessToken": self.creds.access_token,
                                           "chatgptAccountId": self.creds.account_id})

    def _handle_other(self, message: dict[str, Any]) -> None:
        if message.get("method") == "account/chatgptAuthTokens/refresh" and "id" in message:
            try:
                assert self.creds
                self.creds = auth.refresh(self.profile.home, stale=self.creds.access_token)
                self._send({"id": message["id"], "result": {"accessToken": self.creds.access_token,
                                                             "chatgptAccountId": self.creds.account_id}})
            except auth.AuthError:
                self._send({"id": message["id"], "error": {"code": -32000, "message": "account refresh failed"}})
            return
        super()._handle_other(message)


@contextlib.contextmanager
def _lock(profile: profiles.Profile) -> Iterator[None]:
    import fcntl
    digest = hashlib.sha256(str(profile.home.resolve()).encode()).hexdigest()[:16]
    with open(paths.state_dir("locks") / f"redeem-{digest}.lock", "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise appserver.AppServerError("a reset redemption for this account is already running") from None
        yield


def consume(profile: profiles.Profile, idempotency_key: str | None = None,
            credit_id: str | None = None) -> dict[str, Any]:
    if idempotency_key is not None and not idempotency_key.strip():
        raise appserver.AppServerError("idempotency key must not be empty")
    if credit_id is not None and not credit_id.strip():
        raise appserver.AppServerError("credit id must not be empty")
    with _lock(profile), tempfile.TemporaryDirectory(prefix="mycodex-account-") as scratch:
        key = idempotency_key or state.reset_attempt(profile.name)
        params = {"idempotencyKey": key}
        if credit_id:
            params["creditId"] = credit_id
        with AccountServer(Path(scratch), profile) as server:
            response = check.object_value(server.result("account/rateLimitResetCredit/consume", params), "reset reply")
            outcome = response.get("outcome")
            if outcome not in ("reset", "alreadyRedeemed", "nothingToReset", "noCredit"):
                raise appserver.AppServerError("reset redemption returned an unknown outcome; retry with the same key")
            snapshot = server.result("account/rateLimits/read")
        q = quota.from_rpc(profile.name, snapshot)
        expected = auth.claims(profile.home).get("account_id")
        if q.account_id and expected and q.account_id != expected:
            raise appserver.AppServerError("reset snapshot belongs to a different account; pause was left unchanged")
        state.save_quota(profile.name, quota.to_dict(q))
        if outcome in ("reset", "alreadyRedeemed") and q.eligible:
            state.clear_quota_pause(profile.name)
        if idempotency_key is None:
            state.finish_reset(profile.name)
        return {"profile": profile.name, "outcome": outcome, "idempotency_key": key, "quota": quota.to_dict(q)}


def redeem(name: str | None = None, as_json: bool = False, idempotency_key: str | None = None,
           credit_id: str | None = None) -> int:
    cfg = config.load()
    target = name or profiles.active_name(cfg)
    if not target:
        ui.error("choose an account: mycodex quota redeem PROFILE")
        return 1
    target = resolve.resolve(target, profiles.names(cfg), cfg)
    profile = profiles.get(target, cfg)
    assert profile
    try:
        result = consume(profile, idempotency_key, credit_id)
    except (appserver.AppServerError, auth.AuthError, OSError, check.InvalidData) as exc:
        ui.error(f"redemption failed: {exc}", ["retry the same command; pending attempts reuse their idempotency key"])
        return 1
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        ui.panel("Mycodex Quota Redemption", [("Profile", target), ("Outcome", result["outcome"]),
                                               ("Quota", quota.from_dict(result["quota"]).summary()),
                                               ("Attempt", result["idempotency_key"])])
    return 0 if result["outcome"] in ("reset", "alreadyRedeemed") else 1


def try_auto(profile: profiles.Profile) -> bool:
    """Only an exhausted 5h window with an earned credit qualifies for automatic use."""
    q = quota.fetch(profile)
    short = [w for w in q.windows if w.seconds == 18000]
    if not q.ok or not short or short[0].remaining > 0 or not q.reset_credits \
            or any(w.remaining <= 0 for w in q.windows if w.seconds != 18000):
        return False
    result = consume(profile)
    return result["outcome"] in ("reset", "alreadyRedeemed") and quota.from_dict(result["quota"]).eligible
