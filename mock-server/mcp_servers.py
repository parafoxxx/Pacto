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
        return await tg("sendMessage", chat_id=chat_id, text=text)

    @every_server()
    async def telegram_send_voice(chat_id: str, audio_url: str, caption: str = "") -> dict:
        """REAL Telegram: send a voice note to a person (chat_id). Use the audio_url returned by speak
        (Gnani). Calls the Telegram Bot API sendVoice, falling back to sendAudio."""
        res = await tg("sendVoice", chat_id=chat_id, voice=audio_url, caption=caption)
        if not res.get("ok"):
            res = await tg("sendAudio", chat_id=chat_id, audio=audio_url, caption=caption)
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
        result = {"chat_id": chat_id, "messages": out,
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
