"""
MCP connectors for AgenticOrg ("Register Connector" with the MCP toggle on).

Three MCP servers are mounted on the same service:
  /pinelabs/mcp   Pine Labs subscriptions and payouts (mock), plus NEW guardian authority
  /delhivery/mcp  Delhivery pincode, shipment, tracking (mock), plus NEW presence check
  /gnani/mcp      Gnani speech-to-text and text-to-speech (REAL Gnani API), plus NEW wellbeing signal
Each also has an SSE variant at /<name>-sse/sse in case the platform only speaks SSE.

Every tool returns the partner API's response exactly as the REST endpoint returns it,
so the agent sees the same fields the real API would send, including the bad ones.
"""
import base64
import json
import os
import uuid
from typing import Any, Dict, Optional
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

NO_HOST_CHECK = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_mcp(app, state: Dict[str, Any]):
    internal = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal", timeout=60)
    token_cache: Dict[str, str] = {}

    async def call(method: str, path: str, *, bearer=False, token=False, gnani=False, **kw) -> Dict[str, Any]:
        headers = kw.pop("headers", {})
        if bearer:
            if "t" not in token_cache:
                r = await internal.post("/api/auth/v1/token", json={"client_id": "pacto", "client_secret": "pacto"})
                token_cache["t"] = r.json()["access_token"]
            headers["Authorization"] = f"Bearer {token_cache['t']}"
        if token:
            headers["Authorization"] = "Token pacto-mock"
        if gnani:
            headers["X-API-Key-ID"] = "pacto-mock"
        try:
            r = await internal.request(method, path, headers=headers, **kw)
        except httpx.TimeoutException:
            return {"error": "TIMEOUT", "message": "The partner API did not respond in time. Check state before retrying."}
        try:
            body = r.json()
        except Exception:
            return {"http_status": r.status_code, "error": "MALFORMED_RESPONSE",
                    "raw_response": r.text[:300],
                    "message": "The partner API returned a response that is not valid JSON. Do not assume success."}
        if r.status_code >= 400 and isinstance(body, dict):
            body = {"http_status": r.status_code, **(body.get("detail") if isinstance(body.get("detail"), dict) else body)}
        return body

    def money(paise: int) -> Dict[str, Any]:
        return {"value": int(paise), "currency": "INR"}

    # ------------------------------------------------------------------ Pine Labs
    pine = FastMCP("pinelabs-pacto", instructions=(
        "Pine Labs subscriptions (UPI Autopay stakes) and payouts for Pacto. Amounts are in paise "
        "(Rs 200 = 20000). Tools marked NEW are capabilities Pine Labs does not offer today."),
        stateless_http=True, json_response=True, transport_security=NO_HOST_CHECK)
    dl = FastMCP("delhivery-pacto", instructions=(
        "Delhivery shipping for Pacto's protein rewards (mock of Delhivery's documented APIs), plus a NEW presence check."),
        stateless_http=True, json_response=True, transport_security=NO_HOST_CHECK)
    gn = FastMCP("gnani-pacto", instructions=(
        "Gnani Vachana speech for Pacto. transcribe_voice_note and speak call the REAL Gnani API. "
        "wellbeing_signal is NEW (not offered by Gnani today)."),
        stateless_http=True, json_response=True, transport_security=NO_HOST_CHECK)
    SERVERS = [pine, dl, gn]

    def every_server():
        """Register a tool on all three MCP servers, so a call routed to any of our
        connector names finds it (the platform may file all tools under one name)."""
        def wrap(fn):
            for server in SERVERS:
                server.add_tool(fn)
            return fn
        return wrap

    @every_server()
    async def create_plan(plan_name: str, amount_paise: int, max_amount_paise: int) -> dict:
        """Create a subscription plan for a user's stake, charged only when presented ("AS" frequency).
        Calls POST /ps/api/v1/public/plans."""
        return await call("POST", "/ps/api/v1/public/plans", bearer=True, json={
            "plan_name": plan_name, "plan_description": "Pacto stake, charged only on a verified miss",
            "frequency": "AS", "amount": money(amount_paise), "max_limit_amount": money(max_amount_paise),
            "merchant_plan_reference": "pacto-" + uuid.uuid4().hex[:8]})

    @every_server()
    async def create_subscription(plan_id: str, user_name: str, user_vpa: str, merchant_subscription_reference: str,
                                  non_revocable: bool = True) -> dict:
        """Register the user's UPI Autopay mandate against a plan. The user approves it once in their UPI app.
        Calls POST /ps/api/v1/public/subscriptions."""
        return await call("POST", "/ps/api/v1/public/subscriptions", bearer=True, json={
            "plan_id": plan_id, "merchant_subscription_reference": merchant_subscription_reference,
            "customer": {"name": user_name, "vpa": user_vpa}, "allowed_payment_methods": ["UPI"],
            "non_revocable": non_revocable})

    @every_server()
    async def get_subscription(subscription_id: str) -> dict:
        """Get a subscription's current status (ACTIVE, PAUSED, CANCELLED, HALTED) and mandate details.
        Calls GET /ps/api/v1/public/subscriptions/{subscription_id}."""
        return await call("GET", f"/ps/api/v1/public/subscriptions/{subscription_id}", bearer=True)

    @every_server()
    async def create_presentation(subscription_id: str, amount_paise: int, merchant_presentation_reference: str,
                                  due_date: Optional[str] = None) -> dict:
        """Request a charge against the mandate after a verified missed session. Nothing is debited yet.
        merchant_presentation_reference must be at most 50 characters.
        Calls POST /ps/api/v1/public/subscriptions/{subscription_id}/presentations."""
        body = {"amount": money(amount_paise), "merchant_presentation_reference": merchant_presentation_reference}
        if due_date:
            body["due_date"] = due_date
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/presentations", bearer=True, json=body)

    @every_server()
    async def send_subscription_notification(presentation_id: str) -> dict:
        """Send the user the pre-debit notification. The debit is allowed only after debit_allowed_after.
        Calls POST /ps/api/v1/public/subscriptions/notify."""
        return await call("POST", "/ps/api/v1/public/subscriptions/notify", bearer=True, json={"presentation_id": presentation_id})

    @every_server()
    async def create_debit(presentation_id: str) -> dict:
        """Execute the debit after the notice window. Returns status COMPLETED, FAILED (with failure_reason) or an error.
        Calls POST /ps/api/v1/public/subscriptions/execute."""
        return await call("POST", "/ps/api/v1/public/subscriptions/execute", bearer=True, json={"presentation_id": presentation_id})

    @every_server()
    async def create_merchant_retry(presentation_id: str) -> dict:
        """Retry a FAILED debit. At most 3 retries; after that the subscription becomes HALTED.
        Calls POST /ps/api/v1/mandate/merchant-retry."""
        return await call("POST", "/ps/api/v1/mandate/merchant-retry", bearer=True, json={"presentation_id": presentation_id})

    @every_server()
    async def get_presentation_by_merchant_reference(merchant_presentation_reference: str) -> dict:
        """Find a charge request by its reference (Pacto uses "miss-<session_id>"), to see its status
        (CREATED, PENDING, COMPLETED, FAILED, DELETED) and debit_allowed_after.
        Calls GET /ps/api/v1/public/presentations/reference/{merchant_presentation_reference}."""
        return await call("GET", f"/ps/api/v1/public/presentations/reference/{merchant_presentation_reference}", bearer=True)

    @every_server()
    async def delete_presentation(presentation_id: str) -> dict:
        """Withdraw a pending charge request (for example while a dispute is checked).
        Calls DELETE /ps/api/v1/public/presentations/{presentation_id}."""
        return await call("DELETE", f"/ps/api/v1/public/presentations/{presentation_id}", bearer=True)

    @every_server()
    async def resume_subscription(subscription_id: str) -> dict:
        """Resume a paused subscription. Calls POST /ps/api/v1/public/subscriptions/{subscription_id}/resume."""
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/resume", bearer=True)

    @every_server()
    async def verify_upi_id(vpa: str) -> dict:
        """Check that a UPI ID is valid before paying out to it. Calls POST /payment-option."""
        return await call("POST", "/payment-option", bearer=True, json={"payment_method": "UPI", "vpa": vpa})

    @every_server()
    async def create_payout(client_reference_id: str, payee_name: str, vpa: str, amount_paise: int, remarks: str = "") -> dict:
        """Pay a forfeited stake to the Insurer, a charity or the juice shop over UPI. Reusing the same
        client_reference_id never pays twice. Calls POST /payouts/v3/payments/banks."""
        return await call("POST", "/payouts/v3/payments/banks", bearer=True, json={
            "clientReferenceId": client_reference_id, "payeeName": payee_name, "amount": money(amount_paise),
            "mode": "UPI", "vpa": vpa, "remarks": remarks})

    @every_server()
    async def get_payouts(client_reference_id: str) -> dict:
        """Check a payout's status (SCHEDULED, SUCCESS, FAILED). Calls GET /payouts/v3/payments."""
        return await call("GET", "/payouts/v3/payments", bearer=True, params={"clientReferenceId": client_reference_id})

    @every_server()
    async def add_guardian(subscription_id: str, name: str, phone: str, vpa: str, override_codes: int = 4) -> dict:
        """NEW (not offered by Pine Labs today): make the Insurer the guardian of a non-revocable mandate.
        Only the guardian can approve pausing or cancelling it; the user gets a few emergency override codes.
        Calls POST /ps/api/v1/public/subscriptions/{subscription_id}/guardian."""
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/guardian", bearer=True,
                          json={"name": name, "phone": phone, "vpa": vpa, "override_codes": override_codes,
                                "authorities": ["PAUSE", "CANCEL"]})

    @every_server()
    async def request_guardian_decision(subscription_id: str, request_type: str, reason: str,
                                        presentation_id: Optional[str] = None) -> dict:
        """NEW: ask the guardian (Insurer) to approve a PAUSE or CANCEL. If approved, the pending charge
        (presentation_id) is withdrawn. Calls POST .../subscriptions/{subscription_id}/guardian/requests."""
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests", bearer=True,
                          json={"type": request_type, "reason": reason, "presentation_id": presentation_id})

    @every_server()
    async def get_guardian_requests(subscription_id: str) -> dict:
        """NEW: list the guardian, emergency codes left, and every pause/cancel request with its status
        (PENDING_GUARDIAN, APPROVED, REJECTED, OVERRIDDEN). Calls GET .../subscriptions/{subscription_id}/guardian/requests."""
        return await call("GET", f"/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests", bearer=True)

    @every_server()
    async def record_guardian_decision(subscription_id: str, request_id: str, decision: str, guardian_phone: str) -> dict:
        """NEW: record the guardian's APPROVE or REJECT. Fails with NOT_GUARDIAN if guardian_phone is not
        the registered guardian. Calls POST .../guardian/requests/{request_id}/decision."""
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests/{request_id}/decision",
                          bearer=True, json={"decision": decision, "guardian_phone": guardian_phone})

    @every_server()
    async def use_emergency_code(subscription_id: str, request_id: str) -> dict:
        """NEW: overturn a REJECTED guardian request with one of the user's emergency codes. Money is protected;
        returns codes left. Calls POST .../guardian/requests/{request_id}/override."""
        return await call("POST", f"/ps/api/v1/public/subscriptions/{subscription_id}/guardian/requests/{request_id}/override",
                          bearer=True)

    # ------------------------------------------------------------------ Delhivery


    @every_server()
    async def check_pincode_serviceability(pincode: str) -> dict:
        """Check whether Delhivery delivers to a pincode. An empty delivery_codes list means not serviceable.
        Calls GET /c/api/pin-codes/json/?filter_codes=PIN."""
        return await call("GET", "/c/api/pin-codes/json/", token=True, params={"filter_codes": pincode})

    @every_server()
    async def create_shipment(order_id: str, name: str, address: str, pincode: str, phone: str,
                              products_desc: str, hsn_code: str, seller_gst_tin: str, weight_grams: int = 1100) -> dict:
        """Create a prepaid shipment from the brand's warehouse (PACTO-PROTEIN-WH). Returns a waybill on success;
        packages[0].status "Fail" with remarks otherwise (not serviceable, no rider, duplicate order).
        Calls POST /api/cmu/create.json with format=json&data={...}."""
        from urllib.parse import quote_plus
        data = {"shipments": [{"name": name, "add": address, "pin": pincode, "phone": phone, "order": order_id,
                               "payment_mode": "Prepaid", "products_desc": products_desc, "hsn_code": hsn_code,
                               "seller_gst_tin": seller_gst_tin, "total_amount": 0, "weight": weight_grams}],
                "pickup_location": {"name": "PACTO-PROTEIN-WH"}}
        return await call("POST", "/api/cmu/create.json", token=True,
                          content="format=json&data=" + quote_plus(json.dumps(data)),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})

    @every_server()
    async def track_shipment(waybill: str) -> dict:
        """Track a shipment by waybill. Calls GET /api/v1/packages/json/?waybill=AWB."""
        return await call("GET", "/api/v1/packages/json/", token=True, params={"waybill": waybill})

    @every_server()
    async def check_presence(user_id: str, gym_lat: float, gym_lng: float, window_start: str, window_end: str,
                             min_dwell_minutes: int = 45, radius_m: int = 120) -> dict:
        """NEW (not offered by Delhivery today): did the user arrive at the gym and stay? Uses the real GPS
        check-ins the user sent from their phone during the window (ISO times with +05:30).
        verdict: PRESENT, TOO_SHORT, ARRIVED_ONLY or OUTSIDE. Calls POST /api/v1/geofence/presence."""
        return await call("POST", "/api/v1/geofence/presence", token=True, json={
            "user_id": user_id, "window_start": window_start, "window_end": window_end,
            "place": {"lat": gym_lat, "lng": gym_lng, "radius_m": radius_m}, "min_dwell_minutes": min_dwell_minutes})

    # ------------------------------------------------------------------ Gnani (REAL speech)


    def gnani_headers() -> Dict[str, str]:
        h = {"X-API-Key-ID": os.environ.get("GNANI_API_KEY", "")}
        if os.environ.get("GNANI_ORG_ID"):
            h["X-Organization-ID"] = os.environ["GNANI_ORG_ID"]
        if os.environ.get("GNANI_USER_ID"):
            h["X-User-ID"] = os.environ["GNANI_USER_ID"]
        h.update(json.loads(os.environ.get("GNANI_EXTRA_HEADERS", "{}")))
        return h

    async def fetch_audio(url: str) -> tuple:
        headers, auth = {}, None
        if "slack.com" in url and os.environ.get("SLACK_BOT_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['SLACK_BOT_TOKEN']}"
        if "twilio.com" in url and os.environ.get("TWILIO_ACCOUNT_SID"):
            auth = (os.environ["TWILIO_ACCOUNT_SID"], os.environ.get("TWILIO_AUTH_TOKEN", ""))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers=headers, auth=auth)
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "audio/mpeg")

    @every_server()
    async def transcribe_voice_note(audio_url: str, language_code: str = "hi-IN") -> dict:
        """REAL Gnani speech-to-text. Downloads the voice note at audio_url and transcribes it.
        Use language_code hi-IN for Hindi or Hinglish. Calls Gnani POST /stt/v3."""
        if not os.environ.get("GNANI_API_KEY"):
            return {"error": "GNANI_NOT_CONFIGURED", "message": "Set GNANI_API_KEY on the server."}
        try:
            audio, ctype = await fetch_audio(audio_url)
        except Exception as e:
            return {"error": "AUDIO_DOWNLOAD_FAILED", "message": str(e)[:200]}
        url = os.environ.get("GNANI_STT_URL", "https://api.vachana.ai/stt/v3")
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers=gnani_headers(),
                                 files={os.environ.get("GNANI_STT_FILE_FIELD", "audio_file"): ("voice_note.ogg", audio, ctype)}, data={"language_code": language_code})
        except httpx.TimeoutException:
            return {"error": "TIMEOUT", "message": "Gnani speech-to-text timed out"}
        try:
            return {"http_status": r.status_code, **r.json()}
        except Exception:
            return {"http_status": r.status_code, "error": "MALFORMED_RESPONSE", "raw_response": r.text[:300]}

    @every_server()
    async def speak(text: str, voice: str = "Karan", language_code: str = "hi-IN") -> dict:
        """REAL Gnani text-to-speech. Returns audio_url, a public link to the spoken reply that can be sent
        as a voice note or link. Calls Gnani POST /api/v1/tts/sse."""
        if not os.environ.get("GNANI_API_KEY"):
            return {"error": "GNANI_NOT_CONFIGURED", "message": "Set GNANI_API_KEY on the server."}
        url = os.environ.get("GNANI_TTS_URL", "https://api.vachana.ai/api/v1/tts/sse")
        payload = {"text": text, "voice": voice, "model": os.environ.get("GNANI_TTS_MODEL", "vachana-voice-v3"),
                   "language_code": language_code,
                   "audio_config": {"container": "mp3", "encoding": "linear_pcm", "num_channels": 1,
                                    "sample_rate": 44100, "sample_width": 2, "bitrate": "192k"}}
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers={**gnani_headers(), "Content-Type": "application/json"}, json=payload)
        except httpx.TimeoutException:
            return {"error": "TIMEOUT", "message": "Gnani text-to-speech timed out"}
        if r.status_code >= 400:
            return {"http_status": r.status_code, "error": "GNANI_ERROR", "raw_response": r.text[:300]}
        ctype = r.headers.get("content-type", "")
        audio = b""
        if ctype.startswith("audio/"):
            audio = r.content
        else:
            for line in r.text.splitlines():           # SSE ("data: {...}") or a single JSON body
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                chunk = obj.get("audio") or obj.get("audio_content") or (obj.get("data") or {}).get("audio") \
                    if isinstance(obj, dict) else None
                if isinstance(chunk, str):
                    audio += base64.b64decode(chunk)
        if not audio:
            return {"http_status": r.status_code, "error": "NO_AUDIO_IN_RESPONSE", "raw_response": r.text[:300]}
        aid = uuid.uuid4().hex[:16]
        state["audio"][aid] = (audio, "audio/mpeg")
        base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        return {"http_status": r.status_code, "audio_url": f"{base}/audio/{aid}.mp3", "bytes": len(audio)}

    @every_server()
    async def wellbeing_signal(transcript: str, language_code: str = "hi-IN") -> dict:
        """NEW (not offered by Gnani today): does the speaker sound like they are struggling? Returns signal
        STRUGGLING or OK with the cues found. Calls POST /api/v1/insights/wellbeing."""
        return await call("POST", "/api/v1/insights/wellbeing", gnani=True,
                          json={"transcript": transcript, "language_code": language_code})

    # ------------------------------------------------------------------ Streaks and coins
    @every_server()
    async def record_session_outcome(user_id: str, session_id: str, outcome: str) -> dict:
        """NEW (Delhivery presence): record a session's outcome: ATTENDED, MISSED, PAUSED or CODE_USED.
        Returns current_streak, best_streak and coins_to_award (with coin_reference). Streak breaks on MISSED
        or CODE_USED; PAUSED leaves it unchanged. Calls POST /api/v1/geofence/attendance."""
        return await call("POST", "/api/v1/geofence/attendance", token=True,
                          json={"user_id": user_id, "session_id": session_id, "outcome": outcome})

    @every_server()
    async def get_streak(user_id: str) -> dict:
        """NEW (Delhivery presence): a user's current and best streak and every recorded session.
        Calls GET /api/v1/geofence/attendance/{user_id}."""
        return await call("GET", f"/api/v1/geofence/attendance/{user_id}", token=True)

    @every_server()
    async def load_coins(wallet_id: str, coins: int, reference: str) -> dict:
        """Pine Labs Brand Wallet: add coins to the user's closed-loop wallet. Use the coin_reference from
        record_session_outcome; the same reference never loads twice. Calls POST /payment-option/wallet/load."""
        return await call("POST", "/payment-option/wallet/load", bearer=True,
                          json={"wallet_id": wallet_id, "amount": {"value": int(coins), "currency": "COINS"},
                                "reference": reference})

    @every_server()
    async def get_coin_balance(wallet_id: str) -> dict:
        """Pine Labs Brand Wallet: the user's coin balance. Calls POST /payment-option/wallet/balance."""
        return await call("POST", "/payment-option/wallet/balance", bearer=True, json={"wallet_id": wallet_id})

    # ------------------------------------------------------------------ Pine Labs Plural (REAL UAT API)
    pl_token: Dict[str, Any] = {}

    def pl_base() -> str:
        return os.environ.get("PINELABS_BASE_URL", "https://pluraluat.v2.pinepg.in").rstrip("/")

    def pl_headers(token: Optional[str] = None) -> Dict[str, str]:
        from datetime import timezone as _tz
        h = {"Content-Type": "application/json", "Request-ID": str(uuid.uuid4()),
             "Request-Timestamp": datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def pl_auth() -> Any:
        import time as _t
        if pl_token.get("token") and pl_token.get("exp", 0) > _t.time() + 60:
            return pl_token["token"]
        cid, sec = os.environ.get("PINELABS_CLIENT_ID"), os.environ.get("PINELABS_CLIENT_SECRET")
        if not cid or not sec:
            return {"error": "PINELABS_NOT_CONFIGURED", "message": "Set PINELABS_CLIENT_ID and PINELABS_CLIENT_SECRET on the server."}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{pl_base()}/api/auth/v1/token", headers=pl_headers(),
                                 json={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})
            body = r.json()
        except Exception as e:
            return {"error": "PINELABS_AUTH_FAILED", "message": str(e)[:200]}
        if r.status_code != 200 or "access_token" not in body:
            return {"error": "PINELABS_AUTH_FAILED", "http_status": r.status_code, "response": body}
        pl_token.update(token=body["access_token"], exp=_t.time() + 50 * 60)
        return pl_token["token"]

    async def pl_call(method: str, path: str, body: Optional[dict] = None) -> dict:
        tok = await pl_auth()
        if isinstance(tok, dict):
            return tok
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, f"{pl_base()}{path}", headers=pl_headers(tok), json=body)
        except httpx.TimeoutException:
            return {"error": "TIMEOUT", "message": "Pine Labs did not respond in time. Check the link status before retrying."}
        try:
            data = r.json()
        except Exception:
            return {"http_status": r.status_code, "error": "MALFORMED_RESPONSE", "raw_response": r.text[:300]}
        return {"http_status": r.status_code, **data} if isinstance(data, dict) else {"http_status": r.status_code, "data": data}

    @every_server()
    async def pinelabs_create_payment_link(amount_paise: int, merchant_payment_link_reference: str, description: str,
                                           customer_name: str = "", customer_phone: str = "",
                                           customer_email: str = "", expire_minutes: int = 120) -> dict:
        """REAL Pine Labs (Plural UAT): create a payment link to collect a stake. amount_paise e.g. 20000 = Rs 200.
        Use reference "stake-<session_id>" (unique). Returns payment_link_id and the link URL to send the user.
        Calls POST /api/pay/v1/paymentlink."""
        from datetime import timezone as _tz
        body: Dict[str, Any] = {
            "amount": {"value": int(amount_paise), "currency": "INR"},
            "description": description,
            "merchant_payment_link_reference": merchant_payment_link_reference,
            "expire_by": (datetime.now(_tz.utc) + timedelta(minutes=int(expire_minutes))).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        from app import canon as _canon
        merchant_payment_link_reference = _canon(merchant_payment_link_reference)
        prior = state.setdefault("pl_links", {}).get(merchant_payment_link_reference)
        if prior and prior.get("payment_link_id"):
            current = await pl_call("GET", f"/api/pay/v1/paymentlink/{prior['payment_link_id']}")
            if str(current.get("status", "")).upper() not in ("EXPIRED", "CANCELLED", "FAILED"):
                return {**current, "already_created": True,
                        "note": "A link with this reference already exists; reuse it, do not send a new one."}
        cust = {k: v for k, v in {"first_name": customer_name, "mobile_number": customer_phone,
                                    "email_id": customer_email, "country_code": "91" if customer_phone else ""}.items() if v}
        if cust:
            body["customer"] = cust
        res = await pl_call("POST", "/api/pay/v1/paymentlink", body)
        if res.get("payment_link_id"):
            url = res.get("payment_link") or res.get("payment_link_url") or res.get("url") or res.get("short_url")
            entry = {"payment_link_id": res["payment_link_id"], "url": url, "amount_paise": int(amount_paise), "sent": False}
            state.setdefault("pl_links", {})[merchant_payment_link_reference] = entry
            chat = os.environ.get("SEED_USER_TELEGRAM_CHAT_ID", "")
            if chat and url:
                text = (f"Session miss hua. Rs {int(amount_paise) // 100} ka stake abhi is Pine Labs link se pay karo: {url}")
                sent = await tg("sendMessage", chat_id=chat, text=text)
                if sent.get("ok"):
                    entry["sent"] = True
                    state.setdefault("tg_sent", []).append({"chat_id": str(chat), "date": datetime.now(IST).isoformat(timespec="seconds"), "text": text})
            res["user_notified"] = entry["sent"]
        return res

    @every_server()
    async def pinelabs_get_payment_link(payment_link_id: str) -> dict:
        """REAL Pine Labs (Plural UAT): get a payment link's status: CREATED, CLICKED, PAYMENT_INITIATED,
        PROCESSED (paid), CANCELLED or EXPIRED. Calls GET /api/pay/v1/paymentlink/{payment_link_id}."""
        return await pl_call("GET", f"/api/pay/v1/paymentlink/{payment_link_id}")

    @every_server()
    async def pinelabs_cancel_payment_link(payment_link_id: str) -> dict:
        """REAL Pine Labs (Plural UAT): cancel an unpaid payment link (e.g. the Insurer accepted a pause or proof).
        Calls PUT /api/pay/v1/paymentlink/{payment_link_id}/cancel."""
        return await pl_call("PUT", f"/api/pay/v1/paymentlink/{payment_link_id}/cancel")

    @every_server()
    async def get_focus_sessions(user_id: str, since: Optional[str] = None) -> dict:
        """NEW (presence and activity): Forest-style focus sessions the user ran on the Pacto focus page
        (/focus/<user_id>). status is COMPLETED (stayed on the page for the full time), FAILED (left the page
        for more than 10 seconds), ABANDONED (page closed) or RUNNING. since filters by start time (ISO, +05:30).
        Calls GET /api/v1/focus/{user_id}."""
        params = {"since": since} if since else None
        return await call("GET", f"/api/v1/focus/{user_id}", params=params)

    @every_server()
    async def get_walks(user_id: str, since: Optional[str] = None) -> dict:
        """NEW (presence and activity): GPS walks the user recorded on the Pacto walk page (/walk/<user_id>).
        verdict is VERIFIED (goal distance at a plausible walking speed), TOO_SHORT, SUSPICIOUS (vehicle speed or
        implausible average), INSUFFICIENT_DATA or IN_PROGRESS, with distance_km, duration and speeds.
        since filters by start time (ISO, +05:30). Calls GET /api/v1/walk/{user_id}."""
        params = {"since": since} if since else None
        return await call("GET", f"/api/v1/walk/{user_id}", params=params)

    @every_server()
    async def get_session_state(user_id: str, session_start: str, user_chat_id: str = "", insurer_chat_id: str = "") -> dict:
        """START HERE for every session. Gives the session's canonical session_id, its current stage, the next step,
        the user's and Insurer's Telegram replies since Pacto first messaged about it, and what Pacto already sent.
        session_start is the calendar event's start (ISO, +05:30). Pass the user's and Insurer's Telegram chat_ids."""
        from app import canon as _canon
        user_chat_id = str(user_chat_id or os.environ.get("SEED_USER_TELEGRAM_CHAT_ID", "")).strip()
        insurer_chat_id = str(insurer_chat_id or os.environ.get("SEED_INSURER_TELEGRAM_CHAT_ID", "")).strip()
        try:
            st = datetime.fromisoformat(session_start.replace("Z", "+00:00"))
            st = st if st.tzinfo else st.replace(tzinfo=IST)
            st = st.astimezone(IST)
        except Exception:
            return {"error": "session_start must be an ISO time, e.g. 2026-10-04T15:00:00+05:30"}
        sid = f"{user_id.lower()}-{st.strftime('%Y-%m-%d-%H%M')}"
        sub_id = f"v1-sub-{user_id.lower()}"
        sub = state["subscriptions"].get(sub_id, {})
        pres = [p for p in state["presentations"].values() if p["merchant_presentation_reference"] in (f"miss-{sid}", f"miss-{sid}-2")]
        pre = ([p for p in pres if p["status"] != "DELETED"] or pres or [None])[-1]
        greqs = [r for r in state["guardian_requests"].values() if r["subscription_id"] == sub_id and pre and
                 r.get("presentation_id") in [p["presentation_id"] for p in pres]]
        greq = greqs[-1] if greqs else None
        payout = next((p for k, p in state["payouts"].items() if p.get("clientReferenceId") == f"forfeit-{sid}" and "#failed#" not in k), None)
        link = state.get("pl_links", {}).get(f"stake-{sid}")
        link_status = None
        if link and link.get("payment_link_id"):
            live = await pl_call("GET", f"/api/pay/v1/paymentlink/{link['payment_link_id']}")
            link_status = str(live.get("status") or live.get("payment_link_status") or "").upper() or None
            link["status"] = link_status
        PAID = ("PROCESSED", "PAID", "SUCCESS", "COMPLETED", "CAPTURED")
        outcome = state["sessions"].get(user_id.lower(), {}).get(sid, {}).get("outcome")
        st_iso = st.isoformat()
        sent_user = [s for s in state.get("tg_sent", []) if user_chat_id and s["chat_id"] == str(user_chat_id) and s["date"] >= st_iso]
        asked_at = sent_user[0]["date"] if sent_user else None
        def replies(chat):
            if not chat:
                return []
            since = asked_at or st_iso
            return [{"date": x["date"], "text": x.get("text"), "transcript": x.get("transcript"), "voice": bool(x.get("voice"))}
                    for x in state["tg_messages"] if str(x["chat_id"]) == str(chat) and x["date"] >= since]
        now_iso = datetime.now(IST).isoformat(timespec="seconds")
        if payout:
            stage, nxt = "PAID_OUT", "Done. Nothing more to do for this session."
        elif outcome == "ATTENDED":
            stage, nxt = "ATTENDED", "Done."
        elif link_status in PAID or (pre and pre["status"] == "COMPLETED"):
            stage, nxt = "PAYMENT_RECEIVED", f"create_payout to the Insurer with client_reference_id forfeit-{sid}, then receipt and messages."
        elif greq and greq["status"] in ("APPROVED", "OVERRIDDEN") and link and link_status not in ("CANCELLED",) + PAID:
            stage, nxt = ("PAUSED" if greq["status"] == "APPROVED" else "CODE_USED"), "pinelabs_cancel_payment_link for the open link, then done."
        elif greq and greq["status"] == "APPROVED":
            stage, nxt = "PAUSED", "Done: pause approved. Record PAUSED once; do not message again."
        elif greq and greq["status"] == "OVERRIDDEN":
            stage, nxt = "CODE_USED", "Done: emergency code used. Record CODE_USED once; do not message again."
        elif link and link_status == "EXPIRED":
            stage, nxt = "LINK_EXPIRED", f"Create one new link: pinelabs_create_payment_link with reference stake-{sid}-2 (it is sent to the user automatically)."
        elif link and link_status not in ("CANCELLED",):
            stage, nxt = "LINK_SENT_WAITING_FOR_PAYMENT", "The user has the Pine Labs link. Wait for payment; act on any new reply (ill -> pause request)."
        elif greq and greq["status"] == "REJECTED":
            stage, nxt = "INSURER_REJECTED", "R6 REJECT: ask the user about a code once, then act on their reply."
        elif greq and greq["status"] == "PENDING_GUARDIAN":
            stage, nxt = "WAITING_FOR_INSURER", "R6: read insurer_replies; APPROVE or REJECT -> record_guardian_decision. Otherwise wait."
        elif pre and pre["status"] == "PENDING":
            ready = now_iso >= (pre.get("debit_allowed_after") or now_iso)
            stage = "READY_TO_COLLECT" if ready and not replies(user_chat_id) else "IN_NOTICE_WINDOW"
            nxt = (f"pinelabs_create_payment_link (stake, reference stake-{sid}); the server sends the link to the user. Tell the Insurer it is pending."
                   if stage == "READY_TO_COLLECT" else
                   "Act on user_replies (ill -> pause request; was there -> proof). Otherwise wait for the notice window.")
        elif pre and pre["status"] == "FAILED":
            stage, nxt = "DEBIT_FAILED", "R8: create_merchant_retry."
        elif pre and pre["status"] == "DELETED":
            stage, nxt = "DISPUTE_OPEN", "R5 WAS_THERE: wait for the Insurer's decision on the proof."
        elif asked_at:
            stage, nxt = "ASKED_WAITING_FOR_REPLY", "R5: act on user_replies. If none and 30 minutes have passed since asked_at, R4."
        else:
            stage, nxt = "NOT_CHECKED", "R2: check attendance."
        return {"session_id": sid, "stage": stage, "next_step": nxt, "now": now_iso, "asked_at": asked_at,
                "charge_reference": f"miss-{sid}", "payout_reference": f"forfeit-{sid}",
                "subscription_status": sub.get("status"),
                "charge_request": pre and {k: pre.get(k) for k in ("presentation_id", "status", "debit_allowed_after", "failure_reason")},
                "insurer_request": greq and {k: greq.get(k) for k in ("request_id", "status", "reason")},
                "payment_link": link, "payment_link_status": link_status, "attendance_recorded": outcome,
                "stake_reference": f"stake-{sid}",
                "user_replies": replies(user_chat_id), "insurer_replies": replies(insurer_chat_id),
                "pacto_already_sent_to_user": [s["text"][:120] for s in sent_user][-5:]}

    # ------------------------------------------------------------------ Telegram (REAL Bot API)
    def tg_base() -> str:
        return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

    async def tg(method: str, **params) -> dict:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return {"ok": False, "error": "TELEGRAM_NOT_CONFIGURED", "description": "Set TELEGRAM_BOT_TOKEN on the server."}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{tg_base()}/bot{token}/{method}", json=params)
            return r.json()
        except httpx.TimeoutException:
            return {"ok": False, "error": "TIMEOUT", "description": "Telegram did not respond in time"}
        except Exception as e:
            return {"ok": False, "error": "TELEGRAM_ERROR", "description": str(e)[:200]}

    async def gnani_stt_bytes(audio: bytes, ctype: str, language_code: str) -> dict:
        if not os.environ.get("GNANI_API_KEY"):
            return {"error": "GNANI_NOT_CONFIGURED"}
        url = os.environ.get("GNANI_STT_URL", "https://api.vachana.ai/stt/v3")
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers=gnani_headers(), files={os.environ.get("GNANI_STT_FILE_FIELD", "audio_file"): ("voice_note.ogg", audio, ctype)},
                                 data={"language_code": language_code})
            return {"http_status": r.status_code, **r.json()}
        except Exception as e:
            return {"error": "GNANI_STT_FAILED", "message": str(e)[:200]}

    async def poll_updates() -> dict:
        """Pull new Telegram updates into the server's message store (getUpdates)."""
        res = await tg("getUpdates", offset=state["tg_offset"], timeout=0,
                       allowed_updates=["message"])
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or res.get("description") or res}
        for u in res.get("result", []) if res.get("ok") else []:
            state["tg_offset"] = max(state["tg_offset"], u["update_id"] + 1)
            m = u.get("message")
            if not m:
                continue
            chat = m.get("chat", {})
            frm = m.get("from", {})
            item = {"message_id": m.get("message_id"), "chat_id": chat.get("id"),
                    "chat_title": chat.get("title") or chat.get("first_name"),
                    "from_id": frm.get("id"), "from_name": frm.get("first_name"),
                    "from_username": frm.get("username"),
                    "date": datetime.fromtimestamp(m.get("date", 0), IST).isoformat(timespec="seconds"),
                    "text": m.get("text") or m.get("caption")}
            v = m.get("voice") or m.get("audio")
            if v:
                item["voice"] = {"file_id": v.get("file_id"), "duration_s": v.get("duration")}
            state["tg_messages"].append(item)
        state["tg_messages"] = state["tg_messages"][-500:]
        return {"ok": True, "new_updates": len(res.get("result", []))}

    state["_poll_telegram"] = poll_updates   # used by the autopilot

    @every_server()
    async def telegram_find_chats() -> dict:
        """REAL Telegram: list the people and groups that have messaged the Pacto bot, with their chat_id.
        Use this once to find a person's chat_id (they must send /start to the bot first)."""
        await poll_updates()
        chats = {}
        for m in state["tg_messages"]:
            chats[m["chat_id"]] = {"chat_id": m["chat_id"], "name": m["chat_title"],
                                   "username": m.get("from_username"), "last_message_at": m["date"]}
        return {"chats": list(chats.values())}

    @every_server()
    async def telegram_send_message(chat_id: str, text: str) -> dict:
        """REAL Telegram: send a text message from the Pacto bot to a person or group (chat_id).
        Calls the Telegram Bot API sendMessage."""
        now = datetime.now(IST)
        norm = " ".join(text.lower().split())[:120]
        for s in reversed(state.get("tg_sent", [])):
            if s["chat_id"] != str(chat_id).strip():
                continue
            try:
                age = (now - datetime.fromisoformat(s["date"])).total_seconds()
            except Exception:
                continue
            if age > 3600:
                break
            if " ".join(s["text"].lower().split())[:120] == norm:
                replies = [{"date": m["date"], "text": m.get("text"), "transcript": m.get("transcript")}
                           for m in state["tg_messages"] if str(m["chat_id"]) == str(chat_id).strip() and m["date"] >= s["date"]]
                return {"ok": False, "error": "DUPLICATE_MESSAGE_BLOCKED",
                        "description": "You already sent this message to this chat at " + s["date"] +
                                       ". Do not ask again. Act on the replies below (use get_session_state).",
                        "replies_since": replies}
        res = await tg("sendMessage", chat_id=chat_id, text=text)
        if res.get("ok"):
            state.setdefault("tg_sent", []).append({"chat_id": str(chat_id), "date": datetime.now(IST).isoformat(timespec="seconds"),
                                                    "text": text})
            state["tg_sent"] = state["tg_sent"][-300:]
        return res

    @every_server()
    async def telegram_send_voice(chat_id: str, audio_url: str, caption: str = "") -> dict:
        """REAL Telegram: send a voice note to a person (chat_id). Use the audio_url returned by speak
        (Gnani). Calls the Telegram Bot API sendVoice, falling back to sendAudio."""
        res = await tg("sendVoice", chat_id=chat_id, voice=audio_url, caption=caption)
        if not res.get("ok"):
            res = await tg("sendAudio", chat_id=chat_id, audio=audio_url, caption=caption)
        if res.get("ok"):
            state.setdefault("tg_sent", []).append({"chat_id": str(chat_id), "date": datetime.now(IST).isoformat(timespec="seconds"),
                                                    "text": f"[voice note] {caption}".strip()})
        return res

    @every_server()
    async def telegram_get_replies(chat_id: str, since: Optional[str] = None, transcribe: bool = True) -> dict:
        """REAL Telegram + REAL Gnani: get messages a person sent to the Pacto bot in this chat, newest last.
        since is an ISO time (+05:30); only messages after it are returned. Voice notes are downloaded from
        Telegram and transcribed with Gnani (language hi-IN), returned in the transcript field."""
        poll = await poll_updates()
        since_dt, since_note = None, None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=IST)
            except Exception:
                since_note = f"Ignored since={since!r}: not an ISO time"
        in_chat = [m for m in state["tg_messages"] if str(m["chat_id"]).strip() == str(chat_id).strip()]
        selected = [m for m in in_chat if not since_dt or datetime.fromisoformat(m["date"]) > since_dt]
        out = []
        for m in selected:
            item = dict(m)
            if transcribe and m.get("voice") and "transcript" not in m:
                f = await tg("getFile", file_id=m["voice"]["file_id"])
                if f.get("ok"):
                    path = f["result"]["file_path"]
                    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                    try:
                        async with httpx.AsyncClient(timeout=30) as c:
                            a = await c.get(f"{tg_base()}/file/bot{token}/{path}")
                        stt = await gnani_stt_bytes(a.content, "audio/ogg", "hi-IN")
                        m["transcript"] = stt.get("transcript") or stt.get("text") or stt
                    except Exception as e:
                        m["transcript"] = {"error": "VOICE_DOWNLOAD_FAILED", "message": str(e)[:200]}
                else:
                    m["transcript"] = {"error": "GETFILE_FAILED", "detail": f}
                item["transcript"] = m["transcript"]
            out.append(item)
        sent = [s for s in state.get("tg_sent", []) if s["chat_id"] == str(chat_id).strip()][-10:]
        result = {"chat_id": chat_id, "messages": out,
                  "pacto_already_sent": sent,
                  "rule": "Do not repeat a message that is already in pacto_already_sent for the same session.",
                  "total_messages_in_this_chat": len(in_chat),
                  "latest_message_at": in_chat[-1]["date"] if in_chat else None,
                  "telegram_poll": poll}
        if since_note:
            result["note"] = since_note
        if in_chat and not out:
            result["note"] = (f"No messages after since={since}. The latest message in this chat is at "
                              f"{in_chat[-1]['date']}: {in_chat[-1].get('text') or '[voice note]'}")
        if not in_chat:
            known = sorted({str(m['chat_id']) for m in state['tg_messages']})
            result["note"] = f"No messages from chat_id {chat_id}. Chats with messages: {known}"
        return result

    return [("pinelabs", pine), ("delhivery", dl), ("gnani", gn)]
