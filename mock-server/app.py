"""
Pacto mock server (The Ken Case-Build Competition, Round 3).

Mocks the partner endpoints Pacto needs where no real connector exists, using the
endpoint paths and field names from each partner's documentation:

  Pine Labs  (use the platform's real Pine Labs connector wherever it works;
              these mocks are the fallback)
    POST   /api/auth/v1/token
    POST   /ps/api/v1/public/plans
    POST   /ps/api/v1/public/subscriptions
    GET    /ps/api/v1/public/subscriptions/{subscription_id}
    POST   /ps/api/v1/public/subscriptions/{subscription_id}/pause | /resume | /cancel
    POST   /ps/api/v1/public/subscriptions/{subscription_id}/presentations
    POST   /ps/api/v1/public/subscriptions/notify
    POST   /ps/api/v1/public/subscriptions/execute
    DELETE /ps/api/v1/public/presentations/{presentation_id}
    POST   /ps/api/v1/mandate/merchant-retry
    POST   /payment-option                      (verify a UPI VPA)
    POST   /payouts/v3/payments/banks           (payout to the Insurer / charity / juice shop)
    GET    /payouts/v3/payments
    GET    /payouts/v3/payments/funding-account

  Delhivery
    GET    /c/api/pin-codes/json/?filter_codes=PIN
    POST   /api/cmu/create.json                 (format=json&data={...})
    GET    /api/v1/packages/json/?waybill=AWB

  Three capabilities that don't exist today (clearly marked NEW):
    NEW Pine Labs  POST /ps/api/v1/public/subscriptions/{id}/guardian
                   POST /ps/api/v1/public/subscriptions/{id}/guardian/requests
                   POST /ps/api/v1/public/subscriptions/{id}/guardian/requests/{rid}/decision
                   POST /ps/api/v1/public/subscriptions/{id}/guardian/requests/{rid}/override
    NEW Delhivery  POST /api/v1/geofence/presence
    NEW Gnani      POST /api/v1/insights/wellbeing

  Test controls (not part of any partner API; used by the team to force failures):
    POST /_mock/scenario   {"endpoint": "pinelabs.execute", "modes": ["low_balance", "timeout"]}
    POST /_mock/settings   {"notice_seconds": 120}
    GET  /_mock/state      GET /_mock/log      POST /_mock/reset

Failure modes any endpoint can be forced into: "timeout" (waits, then 504),
"malformed" (broken JSON), "server_error" (500). Endpoint-specific ones:
pinelabs.execute: "low_balance"; pinelabs.payout: "invalid_vpa";
delhivery.pincode: "not_serviceable"; delhivery.create: "no_rider" (pickup not
assigned) and "duplicate". Failures also happen naturally: a customer VPA containing
"lowbal" has only Rs 50, a payee VPA without "@" or containing "invalid" fails,
pincodes starting with "99" are not serviceable.
"""
import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

IST = timezone(timedelta(hours=5, minutes=30))
app = FastAPI(title="Pacto mock server", version="1.0.0",
              description="Pine Labs and Delhivery mocks plus three new capabilities for Pacto.")


def now() -> datetime:
    return datetime.now(IST)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def new_state() -> Dict[str, Any]:
    return {
        "settings": {"notice_seconds": 120, "timeout_seconds": 12},
        "scenarios": {},
        "plans": {}, "subscriptions": {}, "presentations": {},
        "balances": {},
        "funding_account": 1_000_000,   # paise available for payouts
        "payouts": {},
        "guardians": {}, "guardian_requests": {},
        "orders": {},
        "warehouses": {"PACTO-PROTEIN-WH"},
        "pings": [],       # real GPS check-ins sent from the user's phone
        "audio": {},       # Gnani text-to-speech replies, served at /audio/<id>.mp3
        "tg_offset": 0,    # Telegram getUpdates offset
        "tg_messages": [], # Telegram messages received by the Pacto bot
        "log": [],
    }


S = new_state()


def seed(state: Dict[str, Any]) -> None:
    """Recreate the demo pact on every start, so a sleeping or restarted free server never
    loses it. Values come from environment variables, with safe test defaults."""
    import os
    env = os.environ.get
    user = env("SEED_USER_ID", "mukund")
    plan_id, sub_id = "v1-plan-pacto", f"v1-sub-{user}"
    stake = int(env("SEED_STAKE_PAISE", "20000"))
    vpa = env("SEED_USER_VPA", f"{user}@okaxis")
    state["plans"][plan_id] = {"plan_id": plan_id, "status": "ACTIVE", "plan_name": "Pacto stake", "frequency": "AS",
                               "amount": {"value": stake, "currency": "INR"},
                               "max_limit_amount": {"value": stake * 5, "currency": "INR"}, "created_at": iso()}
    state["balances"].setdefault(vpa, 5_000 if "lowbal" in vpa else 500_000)
    state["subscriptions"][sub_id] = {
        "subscription_id": sub_id, "plan_id": plan_id, "merchant_subscription_reference": f"pacto-{user}",
        "customer": {"name": env("SEED_USER_NAME", user.title()), "vpa": vpa}, "status": "ACTIVE",
        "mandate": {"type": "UPI_AUTOPAY", "non_revocable": True, "max_amount": {"value": stake * 5, "currency": "INR"},
                    "frequency": "AS"},
        "created_at": iso(), "failed_attempts": 0}
    state["guardians"][sub_id] = {
        "guardian_id": f"grd_{user}", "subscription_id": sub_id, "name": env("SEED_INSURER_NAME", "Vaibhav"),
        "phone": env("SEED_INSURER_PHONE", "+910000000000"), "vpa": env("SEED_INSURER_VPA", "vaibhav@okaxis"),
        "authorities": ["PAUSE", "CANCEL"], "override_codes_left": int(env("SEED_OVERRIDE_CODES", "4")),
        "status": "ACTIVE", "created_at": iso()}


seed(S)


# ----------------------------------------------------------------------------- helpers
def err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


async def chaos(key: str) -> Optional[Any]:
    """Pop the next forced mode for this endpoint. Returns a response for generic
    failures, the mode name for endpoint-specific ones, or None."""
    queue = S["scenarios"].get(key) or []
    if not queue:
        return None
    mode = queue.pop(0)
    if mode == "timeout":
        await asyncio.sleep(S["settings"]["timeout_seconds"])
        return err(504, "GATEWAY_TIMEOUT", "Upstream timed out")
    if mode == "malformed":
        return PlainTextResponse('{"status": "COMPL', status_code=200, media_type="application/json")
    if mode == "server_error":
        return err(500, "INTERNAL_SERVER_ERROR", "Something went wrong")
    return mode


def require_bearer(authorization: Optional[str]):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "Bearer token missing"})


def require_token(authorization: Optional[str]):
    if not authorization or not authorization.lower().startswith("token "):
        raise HTTPException(401, detail={"detail": "Authentication credentials were not provided."})


def money(obj: Any) -> int:
    """Pine Labs amounts are {"value": paise, "currency": "INR"}."""
    if isinstance(obj, dict):
        return int(obj.get("value", 0))
    return int(obj or 0)


@app.middleware("http")
async def request_log(request: Request, call_next):
    path = request.url.path
    skip = path.startswith(("/_mock", "/pinelabs", "/delhivery", "/gnani", "/audio", "/checkin"))
    body = "" if skip else (await request.body())[:600].decode("utf-8", "replace")
    started = time.time()
    response = await call_next(request)
    if not skip:
        S["log"].append({"time": iso(), "method": request.method,
                         "path": request.url.path + (("?" + request.url.query) if request.url.query else ""),
                         "status": response.status_code, "ms": int((time.time() - started) * 1000),
                         "request": body})
        S["log"] = S["log"][-500:]
    return response


# ----------------------------------------------------------------------------- test controls
@app.post("/_mock/scenario", tags=["test controls"])
async def set_scenario(payload: Dict[str, Any]):
    S["scenarios"].setdefault(payload["endpoint"], []).extend(payload.get("modes", []))
    return {"endpoint": payload["endpoint"], "queued": S["scenarios"][payload["endpoint"]]}


@app.post("/_mock/settings", tags=["test controls"])
async def settings(payload: Dict[str, Any]):
    S["settings"].update(payload)
    return S["settings"]


@app.post("/_mock/reset", tags=["test controls"])
async def reset():
    S.clear(); S.update(new_state()); seed(S)
    return {"reset": True, "seeded": True}


@app.get("/_mock/state", tags=["test controls"])
async def state():
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in S.items() if k != "log"}


@app.get("/_mock/log", tags=["test controls"])
async def log():
    return S["log"]


# ----------------------------------------------------------------------------- Pine Labs: auth
@app.post("/api/auth/v1/token", tags=["Pine Labs"])
async def token(payload: Dict[str, Any]):
    if not payload.get("client_id") or not payload.get("client_secret"):
        return err(400, "INVALID_REQUEST", "client_id and client_secret are required")
    return {"access_token": "mock_" + uuid.uuid4().hex, "expires_at": iso(now() + timedelta(hours=1))}


# ----------------------------------------------------------------------------- Pine Labs: plans and subscriptions
@app.post("/ps/api/v1/public/plans", tags=["Pine Labs"])
async def create_plan(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    if (r := await chaos("pinelabs.plan")) is not None and not isinstance(r, str):
        return r
    for f in ("plan_name", "frequency", "amount"):
        if f not in payload:
            return err(400, "INVALID_REQUEST", f"{f} is required")
    pid = "v1-plan-" + uuid.uuid4().hex[:12]
    plan = {"plan_id": pid, "status": "ACTIVE", "created_at": iso(), **payload}
    S["plans"][pid] = plan
    return plan


@app.post("/ps/api/v1/public/subscriptions", tags=["Pine Labs"])
async def create_subscription(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    if (r := await chaos("pinelabs.subscription")) is not None and not isinstance(r, str):
        return r
    plan = S["plans"].get(payload.get("plan_id", ""))
    if not plan:
        return err(404, "PLAN_NOT_FOUND", "plan_id does not exist")
    sid = "v1-sub-" + uuid.uuid4().hex[:12]
    customer = payload.get("customer") or {}
    vpa = customer.get("vpa") or payload.get("vpa") or "user@upi"
    S["balances"].setdefault(vpa, 5_000 if "lowbal" in vpa else 500_000)
    sub = {"subscription_id": sid, "plan_id": plan["plan_id"],
           "merchant_subscription_reference": payload.get("merchant_subscription_reference"),
           "customer": {**customer, "vpa": vpa},
           "status": "ACTIVE",           # mock: the user approves the mandate in their UPI app
           "mandate": {"type": "UPI_AUTOPAY", "non_revocable": bool(payload.get("non_revocable", False)),
                       "max_amount": plan.get("max_limit_amount", plan.get("amount")),
                       "frequency": plan.get("frequency")},
           "created_at": iso(), "failed_attempts": 0}
    S["subscriptions"][sid] = sub
    return sub


def get_sub(sid: str) -> Dict[str, Any]:
    sub = S["subscriptions"].get(sid)
    if not sub:
        raise HTTPException(404, detail={"code": "SUBSCRIPTION_NOT_FOUND", "message": "subscription_id does not exist"})
    return sub


@app.get("/ps/api/v1/public/subscriptions/{subscription_id}", tags=["Pine Labs"])
async def get_subscription(subscription_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    return get_sub(subscription_id)


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/pause", tags=["Pine Labs"])
async def pause(subscription_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    sub = get_sub(subscription_id)
    if S["guardians"].get(subscription_id) and "PAUSE" in S["guardians"][subscription_id]["authorities"] \
            and not sub.get("_guardian_pause_ok"):
        return err(403, "GUARDIAN_APPROVAL_REQUIRED", "This subscription can only be paused with the guardian's approval")
    sub["status"] = "PAUSED"; sub["paused_at"] = iso(); sub.pop("_guardian_pause_ok", None)
    return sub


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/resume", tags=["Pine Labs"])
async def resume(subscription_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    sub = get_sub(subscription_id)
    if sub["status"] != "PAUSED":
        return err(409, "INVALID_STATE", f"Subscription is {sub['status']}, not PAUSED")
    sub["status"] = "ACTIVE"; sub["resumed_at"] = iso()
    return sub


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/cancel", tags=["Pine Labs"])
async def cancel(subscription_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    sub = get_sub(subscription_id)
    g = S["guardians"].get(subscription_id)
    if g and "CANCEL" in g["authorities"] and not sub.get("_guardian_cancel_ok"):
        return err(403, "GUARDIAN_APPROVAL_REQUIRED", "This subscription can only be cancelled with the guardian's approval")
    sub["status"] = "CANCELLED"; sub["cancelled_at"] = iso()
    return sub


# ----------------------------------------------------------------------------- Pine Labs: presentations and debits
@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/presentations", tags=["Pine Labs"])
async def create_presentation(subscription_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    if (r := await chaos("pinelabs.presentation")) is not None and not isinstance(r, str):
        return r
    sub = get_sub(subscription_id)
    if sub["status"] != "ACTIVE":
        return err(409, "INVALID_STATE", f"Subscription is {sub['status']}")
    ref = payload.get("merchant_presentation_reference", "")
    if len(ref) > 50:
        return err(400, "INVALID_REQUEST", "merchant_presentation_reference must be at most 50 characters")
    amount = money(payload.get("amount"))
    max_amt = money(sub["mandate"]["max_amount"])
    if amount <= 0 or (max_amt and amount > max_amt):
        return err(400, "AMOUNT_EXCEEDS_MANDATE", "Amount is zero or above the mandate limit")
    pid = "v1-pre-" + uuid.uuid4().hex[:12]
    pre = {"presentation_id": pid, "subscription_id": subscription_id, "merchant_presentation_reference": ref,
           "amount": payload.get("amount"), "due_date": payload.get("due_date", iso()[:10]),
           "status": "CREATED", "created_at": iso(), "attempts": 0}
    S["presentations"][pid] = pre
    return pre


def find_presentation(payload: Dict[str, Any]) -> Dict[str, Any]:
    pid = payload.get("presentation_id")
    if pid and pid in S["presentations"]:
        return S["presentations"][pid]
    ref = payload.get("merchant_presentation_reference")
    for p in S["presentations"].values():
        if ref and p["merchant_presentation_reference"] == ref:
            return p
    raise HTTPException(404, detail={"code": "PRESENTATION_NOT_FOUND", "message": "No such presentation"})


@app.post("/ps/api/v1/public/subscriptions/notify", tags=["Pine Labs"])
async def notify(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    if (r := await chaos("pinelabs.notify")) is not None and not isinstance(r, str):
        return r
    pre = find_presentation(payload)
    if pre["status"] not in ("CREATED",):
        return err(409, "INVALID_STATE", f"Presentation is {pre['status']}")
    pre["status"] = "PENDING"
    pre["notified_at"] = iso()
    pre["debit_allowed_after"] = iso(now() + timedelta(seconds=S["settings"]["notice_seconds"]))
    return {"presentation_id": pre["presentation_id"], "status": "PENDING",
            "message": "Pre-debit notification initiated", "debit_allowed_after": pre["debit_allowed_after"]}


async def attempt_debit(pre: Dict[str, Any], forced: Optional[str]) -> Any:
    sub = S["subscriptions"][pre["subscription_id"]]
    if sub["status"] == "PAUSED":
        pre["status"] = "PAUSED"
        return err(409, "SUBSCRIPTION_PAUSED", "Subscription is paused; no debit taken")
    if sub["status"] in ("CANCELLED", "HALTED"):
        return err(409, "INVALID_STATE", f"Subscription is {sub['status']}")
    if pre["status"] == "COMPLETED":
        return {**pre, "message": "Already debited"}
    if pre.get("debit_allowed_after") is None:
        return err(422, "PRE_DEBIT_NOTIFICATION_REQUIRED", "Send the pre-debit notification first")
    if now() < datetime.fromisoformat(pre["debit_allowed_after"]):
        return err(422, "PRE_DEBIT_NOTIFICATION_WINDOW_ACTIVE",
                   f"Debit allowed only after {pre['debit_allowed_after']}")
    pre["attempts"] += 1
    amount = money(pre["amount"])
    vpa = sub["customer"]["vpa"]
    if forced == "low_balance" or S["balances"].get(vpa, 0) < amount:
        pre["status"] = "FAILED"; pre["failure_reason"] = "INSUFFICIENT_FUNDS"
        sub["failed_attempts"] += 1
        if pre["attempts"] >= 4:   # first attempt plus three retries
            sub["status"] = "HALTED"
        return {**pre, "subscription_status": sub["status"]}
    S["balances"][vpa] -= amount
    S["funding_account"] += amount
    pre["status"] = "COMPLETED"; pre["debited_at"] = iso()
    pre["transaction_id"] = "txn_" + uuid.uuid4().hex[:14]
    return pre


@app.post("/ps/api/v1/public/subscriptions/execute", tags=["Pine Labs"])
async def execute(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    r = await chaos("pinelabs.execute")
    if r is not None and not isinstance(r, str):
        return r
    return await attempt_debit(find_presentation(payload), r)


@app.post("/ps/api/v1/mandate/merchant-retry", tags=["Pine Labs"])
async def merchant_retry(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    pre = find_presentation(payload)
    if pre["status"] != "FAILED":
        return err(409, "INVALID_STATE", "Only a FAILED debit can be retried")
    if pre["attempts"] >= 4:
        return err(429, "RETRY_LIMIT_REACHED", "Maximum 3 retries reached")
    r = await chaos("pinelabs.execute")
    if r is not None and not isinstance(r, str):
        return r
    return await attempt_debit(pre, r)


@app.get("/ps/api/v1/public/presentations/reference/{merchant_presentation_reference}", tags=["Pine Labs"])
async def presentation_by_reference(merchant_presentation_reference: str, authorization: Optional[str] = Header(None)):
    """Find a charge request by its merchant reference (Pine Labs offers get_presentation_by_merchant_reference)."""
    require_bearer(authorization)
    for p in S["presentations"].values():
        if p["merchant_presentation_reference"] == merchant_presentation_reference:
            return p
    return err(404, "PRESENTATION_NOT_FOUND", "No presentation with this reference")


@app.delete("/ps/api/v1/public/presentations/{presentation_id}", tags=["Pine Labs"])
async def delete_presentation(presentation_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    pre = S["presentations"].get(presentation_id)
    if not pre:
        return err(404, "PRESENTATION_NOT_FOUND", "No such presentation")
    if pre["status"] == "COMPLETED":
        return err(409, "INVALID_STATE", "A completed debit cannot be deleted")
    pre["status"] = "DELETED"; pre["deleted_at"] = iso()
    return {"presentation_id": presentation_id, "status": "DELETED"}


# ----------------------------------------------------------------------------- Pine Labs: VPA check and payouts
@app.post("/payment-option", tags=["Pine Labs"])
async def payment_option(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    vpa = (payload.get("vpa") or payload.get("upi_id") or "").strip()
    valid = "@" in vpa and "invalid" not in vpa
    return {"vpa": vpa, "is_valid": valid, "payment_method": "UPI",
            "message": "VPA verified" if valid else "VPA could not be verified"}


@app.post("/payouts/v3/payments/banks", tags=["Pine Labs"])
async def create_payout(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    r = await chaos("pinelabs.payout")
    if r is not None and not isinstance(r, str):
        return r
    ref = payload.get("clientReferenceId")
    if not ref:
        return err(400, "INVALID_REQUEST", "clientReferenceId is required")
    if ref in S["payouts"]:
        return S["payouts"][ref]          # idempotent: same reference never pays twice
    amount = money(payload.get("amount"))
    vpa = payload.get("vpa", "")
    out = {"clientReferenceId": ref, "paymentReferenceId": "PO" + uuid.uuid4().hex[:14].upper(),
           "payeeName": payload.get("payeeName"), "mode": payload.get("mode", "UPI"), "vpa": vpa,
           "amount": payload.get("amount"), "createdAt": iso()}
    if payload.get("mode", "UPI") == "UPI" and (r == "invalid_vpa" or "@" not in vpa or "invalid" in vpa):
        out.update(status="FAILED", failureReason="INVALID_VPA")
    elif amount > S["funding_account"]:
        out.update(status="FAILED", failureReason="INSUFFICIENT_FUNDING_BALANCE")
    else:
        S["funding_account"] -= amount
        out.update(status="SCHEDULED", _success_at=time.time() + 10)
    if out["status"] == "FAILED":
        S["payouts"][ref + "#failed#" + uuid.uuid4().hex[:4]] = out   # a fixed request can reuse the reference
    else:
        S["payouts"][ref] = out
    return {k: v for k, v in out.items() if not k.startswith("_")}


@app.get("/payouts/v3/payments", tags=["Pine Labs"])
async def list_payouts(clientReferenceId: Optional[str] = None, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    items = []
    for key, p in S["payouts"].items():
        if p.get("status") == "SCHEDULED" and time.time() >= p.get("_success_at", 0):
            p["status"] = "SUCCESS"
        if clientReferenceId is None or p["clientReferenceId"] == clientReferenceId:
            items.append({k: v for k, v in p.items() if not k.startswith("_")})
    return {"data": items}


@app.get("/payouts/v3/payments/funding-account", tags=["Pine Labs"])
async def funding(authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    return {"balance": {"value": S["funding_account"], "currency": "INR"}}


# ----------------------------------------------------------------------------- NEW 1 (Pine Labs): guardian-held authority
@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/guardian", tags=["NEW: Pine Labs guardian"])
async def add_guardian(subscription_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    """NEW capability: a named third party (the Insurer) holds authority over pausing
    or cancelling a non-revocable mandate. The payer can request; only the guardian
    decides, and a limited number of override codes let the payer escape an unfair refusal."""
    require_bearer(authorization)
    get_sub(subscription_id)
    g = {"guardian_id": "grd_" + uuid.uuid4().hex[:10], "subscription_id": subscription_id,
         "name": payload.get("name"), "phone": payload.get("phone"), "vpa": payload.get("vpa"),
         "authorities": payload.get("authorities", ["PAUSE", "CANCEL"]),
         "override_codes_left": int(payload.get("override_codes", 4)), "status": "ACTIVE", "created_at": iso()}
    S["guardians"][subscription_id] = g
    return g


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests", tags=["NEW: Pine Labs guardian"])
async def guardian_request(subscription_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    if subscription_id not in S["guardians"]:
        return err(404, "GUARDIAN_NOT_FOUND", "No guardian on this subscription")
    rtype = payload.get("type", "PAUSE")
    if rtype not in ("PAUSE", "CANCEL"):
        return err(400, "INVALID_REQUEST", "type must be PAUSE or CANCEL")
    rid = "grq_" + uuid.uuid4().hex[:10]
    req = {"request_id": rid, "subscription_id": subscription_id, "type": rtype,
           "reason": payload.get("reason"), "evidence": payload.get("evidence"),
           "presentation_id": payload.get("presentation_id"),
           "status": "PENDING_GUARDIAN", "created_at": iso(),
           "expires_at": iso(now() + timedelta(hours=24))}
    S["guardian_requests"][rid] = req
    return req


def apply_request(req: Dict[str, Any]):
    sub = S["subscriptions"][req["subscription_id"]]
    if req["type"] == "PAUSE":
        sub["status"] = "PAUSED"; sub["paused_at"] = iso()
    else:
        sub["status"] = "CANCELLED"; sub["cancelled_at"] = iso()
    pid = req.get("presentation_id")
    if pid and pid in S["presentations"] and S["presentations"][pid]["status"] in ("CREATED", "PENDING", "FAILED"):
        S["presentations"][pid]["status"] = "DELETED"


@app.get("/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests", tags=["NEW: Pine Labs guardian"])
async def list_guardian_requests(subscription_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    g = S["guardians"].get(subscription_id)
    return {"guardian": g, "requests": [r for r in S["guardian_requests"].values() if r["subscription_id"] == subscription_id]}


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests/{request_id}/decision",
          tags=["NEW: Pine Labs guardian"])
async def guardian_decision(subscription_id: str, request_id: str, payload: Dict[str, Any],
                            authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    req = S["guardian_requests"].get(request_id)
    g = S["guardians"].get(subscription_id)
    if not req or not g:
        return err(404, "NOT_FOUND", "No such guardian request")
    if req["status"] != "PENDING_GUARDIAN":
        return err(409, "ALREADY_DECIDED", f"Request is {req['status']}")
    if str(payload.get("guardian_phone", "")).strip() != str(g["phone"]).strip():
        return err(403, "NOT_GUARDIAN", "Only the registered guardian can decide this request")
    decision = payload.get("decision")
    if decision == "APPROVE":
        req["status"] = "APPROVED"; apply_request(req)
    elif decision == "REJECT":
        req["status"] = "REJECTED"
    else:
        return err(400, "INVALID_REQUEST", "decision must be APPROVE or REJECT")
    req["decided_at"] = iso()
    return {**req, "subscription_status": S["subscriptions"][subscription_id]["status"]}


@app.post("/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests/{request_id}/override",
          tags=["NEW: Pine Labs guardian"])
async def guardian_override(subscription_id: str, request_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization)
    req = S["guardian_requests"].get(request_id)
    g = S["guardians"].get(subscription_id)
    if not req or not g:
        return err(404, "NOT_FOUND", "No such guardian request")
    if req["status"] != "REJECTED":
        return err(409, "INVALID_STATE", "Only a rejected request can be overridden")
    if g["override_codes_left"] <= 0:
        return err(403, "NO_OVERRIDE_CODES_LEFT", "No emergency codes left this cycle")
    g["override_codes_left"] -= 1
    req["status"] = "OVERRIDDEN"; req["overridden_at"] = iso(); apply_request(req)
    return {**req, "override_codes_left": g["override_codes_left"],
            "subscription_status": S["subscriptions"][subscription_id]["status"]}


# ----------------------------------------------------------------------------- Delhivery (mock of documented APIs)
PIN_INFO = {
    "208016": ("Kanpur Nagar", "UP"), "208001": ("Kanpur Nagar", "UP"), "110001": ("New Delhi", "DL"),
    "110017": ("South Delhi", "DL"), "400001": ("Mumbai", "MH"), "560001": ("Bengaluru", "KA"),
    "122001": ("Gurugram", "HR"), "500081": ("Hyderabad", "TG"),
}


@app.get("/c/api/pin-codes/json/", tags=["Delhivery"])
async def pincodes(filter_codes: str = "", authorization: Optional[str] = Header(None)):
    require_token(authorization)
    r = await chaos("delhivery.pincode")
    if r is not None and not isinstance(r, str):
        return r
    codes = []
    for pin in [p.strip() for p in filter_codes.split(",") if p.strip()]:
        if r == "not_serviceable" or pin.startswith("99") or len(pin) != 6 or not pin.isdigit():
            continue
        district, state_code = PIN_INFO.get(pin, ("Unknown", "NA"))
        codes.append({"postal_code": {"pin": int(pin), "district": district, "state_code": state_code,
                                      "country_code": "IN", "pre_paid": "Y", "cash": "Y", "cod": "Y",
                                      "pickup": "Y", "repl": "Y", "is_oda": "N",
                                      "sort_code": f"{district[:3].upper()}/{pin[-3:]}"}})
    return {"delivery_codes": codes}


@app.post("/api/cmu/create.json", tags=["Delhivery"])
async def cmu_create(request: Request, authorization: Optional[str] = Header(None)):
    require_token(authorization)
    r = await chaos("delhivery.create")
    if r is not None and not isinstance(r, str):
        return r
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        if raw.startswith("format=json&data="):
            from urllib.parse import unquote_plus
            data = json.loads(unquote_plus(raw[len("format=json&data="):]))
        else:
            data = json.loads(raw)
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "rmk": "Invalid JSON in data. Send format=json&data={...}"})
    shipments = data.get("shipments") or []
    pickup = (data.get("pickup_location") or {}).get("name")
    packages = []
    ok = 0
    for s in shipments:
        remarks = []
        for f in ("name", "add", "pin", "phone", "order"):
            if not s.get(f):
                remarks.append(f"{f} is mandatory")
        if not s.get("seller_gst_tin") or not s.get("hsn_code"):
            remarks.append("seller_gst_tin and hsn_code are mandatory")
        if pickup not in S["warehouses"]:
            remarks.append("ClientWarehouse matching query does not exist.")
        if str(s.get("pin", "")).startswith("99"):
            remarks.append("NSZ: pincode not serviceable")
        if s.get("order") in S["orders"] or r == "duplicate":
            remarks.append("Duplicate order id")
        if r == "no_rider":
            remarks.append("Pickup could not be assigned: no rider available for this slot")
        if remarks:
            packages.append({"status": "Fail", "waybill": "", "refnum": s.get("order"), "remarks": remarks,
                             "serviceable": not any("NSZ" in x for x in remarks)})
            continue
        awb = str(8430371000000 + len(S["orders"]) * 7 + 66)
        S["orders"][s["order"]] = {"waybill": awb, "created": time.time(), "shipment": s, "pickup": pickup}
        packages.append({"status": "Success", "waybill": awb, "refnum": s["order"], "remarks": [],
                         "payment": "Pre-paid", "cod_amount": 0, "serviceable": True,
                         "sort_code": "KNP/" + str(s["pin"])[-3:], "client": "PACTO"})
        ok += 1
    return {"success": ok == len(shipments) and ok > 0, "package_count": len(shipments),
            "upload_wbn": "UPL" + uuid.uuid4().hex[:16].upper(), "prepaid_count": ok, "cod_count": 0,
            "cod_amount": 0, "replacement_count": 0, "pickups_count": 0, "packages": packages}


STAGES = [(0, "Manifested", "UD", "Pickup scheduled"), (60, "In Transit", "UD", "Shipment picked up"),
          (180, "Dispatched", "UD", "Out for delivery"), (300, "Delivered", "DL", "Delivered to consignee")]


@app.get("/api/v1/packages/json/", tags=["Delhivery"])
async def track(waybill: str = "", ref_ids: str = "", authorization: Optional[str] = Header(None)):
    require_token(authorization)
    r = await chaos("delhivery.track")
    if r is not None and not isinstance(r, str):
        return r
    order = next((o for o in S["orders"].values() if o["waybill"] == waybill or o["shipment"].get("order") == ref_ids), None)
    if not order:
        return {"ShipmentData": [], "Error": "No such waybill or Order Id found"}
    age = time.time() - order["created"]
    scans, current = [], STAGES[0]
    for secs, status, stype, instr in STAGES:
        if age >= secs:
            current = (secs, status, stype, instr)
            scans.append({"ScanDetail": {"Scan": status, "ScanType": stype, "Instructions": instr,
                                         "ScanDateTime": iso(datetime.fromtimestamp(order["created"] + secs, IST)),
                                         "ScannedLocation": "Kanpur_Hub (Uttar Pradesh)"}})
    return {"ShipmentData": [{"Shipment": {
        "AWB": order["waybill"], "ReferenceNo": order["shipment"]["order"],
        "Status": {"Status": current[1], "StatusType": current[2], "Instructions": current[3],
                   "StatusDateTime": scans[-1]["ScanDetail"]["ScanDateTime"], "StatusLocation": "Kanpur_Hub (Uttar Pradesh)"},
        "Scans": scans}}]}


# ----------------------------------------------------------------------------- NEW 2 (Delhivery): presence at a place
def metres(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 6_371_000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@app.post("/api/v1/geofence/presence", tags=["NEW: Delhivery presence"])
async def presence(payload: Dict[str, Any], authorization: Optional[str] = Header(None)):
    """NEW capability: given a verified place and timestamped location pings, say whether
    the person arrived and stayed. Builds on Delhivery's geocoded address data and the
    geofencing it already runs for rider arrivals."""
    require_token(authorization)
    r = await chaos("delhivery.presence")
    if r is not None and not isinstance(r, str):
        return r
    place = payload.get("place") or {}
    radius = float(place.get("radius_m", 120))
    pings = payload.get("pings") or []
    if not pings and payload.get("user_id"):
        start = datetime.fromisoformat(payload["window_start"]) if payload.get("window_start") else None
        end = datetime.fromisoformat(payload["window_end"]) if payload.get("window_end") else None
        pings = [p for p in S["pings"] if p["user_id"] == payload["user_id"]
                 and (start is None or datetime.fromisoformat(p["timestamp"]) >= start)
                 and (end is None or datetime.fromisoformat(p["timestamp"]) <= end)]
    if not place:
        return JSONResponse(status_code=400, content={"error": "place is required"})
    if not pings:
        return {"verdict": "OUTSIDE", "dwell_minutes": 0, "min_dwell_minutes": int(payload.get("min_dwell_minutes", 45)),
                "radius_m": radius, "pings": [], "note": "No check-ins received in this window"}
    inside = []
    for p in pings:
        d = metres(float(place["lat"]), float(place["lng"]), float(p["lat"]), float(p["lng"]))
        inside.append({**p, "distance_m": round(d), "inside": d <= radius})
    ins = [p for p in inside if p["inside"]]
    dwell = 0
    if len(ins) >= 2:
        t = sorted(datetime.fromisoformat(p["timestamp"]) for p in ins)
        dwell = int((t[-1] - t[0]).total_seconds() // 60)
    need = int(payload.get("min_dwell_minutes", 45))
    if not ins:
        verdict = "OUTSIDE"
    elif len(ins) == 1:
        verdict = "ARRIVED_ONLY"
    elif dwell >= need:
        verdict = "PRESENT"
    else:
        verdict = "TOO_SHORT"
    return {"verdict": verdict, "dwell_minutes": dwell, "min_dwell_minutes": need, "radius_m": radius, "pings": inside}


# ----------------------------------------------------------------------------- NEW 3 (Gnani): wellbeing signal
STRUGGLE_CUES = ["fail", "haar", "kuch nahi hoga", "koi fayda nahi", "thak gaya", "thak gayi", "give up",
                 "chhod", "useless", "bekar", "depressed", "low feel", "akela", "nahi ho payega", "hopeless"]


@app.post("/api/v1/insights/wellbeing", tags=["NEW: Gnani wellbeing"])
async def wellbeing(payload: Dict[str, Any], x_api_key_id: Optional[str] = Header(None, alias="X-API-Key-ID")):
    """NEW capability: from a transcript (and, in the real version, the voice itself),
    return whether the speaker sounds like they are struggling, so the agent lowers
    pressure instead of raising it. Builds on Gnani's Indian-language speech data."""
    if not x_api_key_id:
        return JSONResponse(status_code=401, content={"error": "X-API-Key-ID header missing"})
    r = await chaos("gnani.wellbeing")
    if r is not None and not isinstance(r, str):
        return r
    import re
    text = (payload.get("transcript") or "").lower()
    cues = [c for c in STRUGGLE_CUES if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", text)]
    score = min(1.0, 0.35 * len(cues))
    return {"request_id": uuid.uuid4().hex, "signal": "STRUGGLING" if score >= 0.35 else "OK",
            "score": round(score, 2), "cues": cues, "language_code": payload.get("language_code", "hi-IN")}


@app.get("/", tags=["info"])
async def root():
    return {"service": "Pacto mock server", "docs": "/docs", "openapi": "/openapi.json", "time": iso()}


# ----------------------------------------------------------------------------- Pacto check-in page (real phone GPS)
@app.post("/api/v1/geofence/pings", tags=["NEW: Delhivery presence"])
async def add_ping(payload: Dict[str, Any]):
    """Stores one real GPS check-in sent from the user's phone by the Pacto check-in page."""
    for f in ("user_id", "lat", "lng"):
        if f not in payload:
            return JSONResponse(status_code=400, content={"error": f"{f} is required"})
    ping = {"user_id": payload["user_id"], "lat": float(payload["lat"]), "lng": float(payload["lng"]),
            "accuracy_m": payload.get("accuracy_m"), "type": payload.get("type", "check_in"), "timestamp": iso()}
    S["pings"].append(ping)
    return ping


CHECKIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pacto check-in</title>
<style>body{font-family:system-ui,sans-serif;max-width:28rem;margin:0 auto;padding:2rem 1.25rem;background:#F4F6F8;color:#18212E}
h1{font-size:2rem;margin:0 0 .25rem}p{color:#56606E}button{display:block;width:100%;font-size:1.25rem;padding:1rem;margin:.75rem 0;
border:0;border-radius:12px;color:#fff;cursor:pointer}#in{background:#1F7A55}#out{background:#18212E}#msg{min-height:3rem;font-weight:600}</style></head>
<body><h1>Pacto</h1><p>Hi __USER__. Tap when you reach the gym and again when you leave. Your location is sent only at the moment you tap.</p>
<button id="in">Check me in</button><button id="out">Check me out</button><p id="msg"></p>
<script>
function send(type){var m=document.getElementById('msg');m.textContent='Getting your location...';
if(!navigator.geolocation){m.textContent='This browser cannot share location.';return;}
navigator.geolocation.getCurrentPosition(function(p){fetch('/api/v1/geofence/pings',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({user_id:'__USER__',lat:p.coords.latitude,lng:p.coords.longitude,accuracy_m:p.coords.accuracy,type:type})})
.then(function(r){return r.json()}).then(function(d){m.textContent=(type==='check_in'?'Checked in':'Checked out')+' at '+d.timestamp.slice(11,16)+'.';})
.catch(function(){m.textContent='Could not send. Try again.';});},function(){m.textContent='Please allow location access and try again.';},
{enableHighAccuracy:true,timeout:15000});}
document.getElementById('in').onclick=function(){send('check_in')};document.getElementById('out').onclick=function(){send('check_out')};
</script></body></html>"""


@app.get("/checkin/{user_id}", tags=["Pacto app"])
async def checkin_page(user_id: str):
    from fastapi.responses import HTMLResponse
    import html
    return HTMLResponse(CHECKIN_HTML.replace("__USER__", html.escape(user_id)))


@app.get("/audio/{audio_id}.mp3", tags=["Pacto app"])
async def audio(audio_id: str):
    from fastapi.responses import Response
    item = S["audio"].get(audio_id)
    if not item:
        raise HTTPException(404, detail="No such audio")
    return Response(content=item[0], media_type=item[1])


# ----------------------------------------------------------------------------- MCP connectors for AgenticOrg
import contextlib
from mcp_servers import build_mcp

MCP_SERVERS = build_mcp(app, S)


@contextlib.asynccontextmanager
async def _lifespan(_app):
    async with contextlib.AsyncExitStack() as stack:
        for _, server in MCP_SERVERS:
            await stack.enter_async_context(server.session_manager.run())
        yield


app.router.lifespan_context = _lifespan
for _name, _server in MCP_SERVERS:
    app.mount(f"/{_name}", _server.streamable_http_app())          # https://<host>/<name>/mcp
    app.mount(f"/{_name}-sse", _server.sse_app())   # https://<host>/<name>-sse/sse
