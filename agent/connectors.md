# Pacto connectors on AgenticOrg

The brief's rule: Gnani is real, Delhivery is our mock, Pine Labs is real wherever the platform has a working connector, every other tool is the real thing, and up to 3 capabilities that don't exist today may live on our mock server.

## Native connectors (real)

| Connector | Real or mock | Used for | Tools |
|---|---|---|---|
| **Slack** | Real | Conversations with the user and the Insurer: missed-session questions, voice clips, APPROVE/REJECT, code consent | send_message, search_messages |
| **WhatsApp** | Real | Notices to the circle: stake charged, code used, quit | send_text_message, send_media_message |
| **Gmail** | Real | Receipt to the user when a stake is charged | send_email |
| **Google Calendar** | Real | The user's gym sessions ("Pacto gym: rahul") | list_events |
| **Pine Labs Plural** | **Real** (Pine Labs UAT) | A payment link when Autopay fails three times; checking it was paid; refunding it if the charge is overturned | create_payment_link, get_order_status, initiate_refund |

The native Pine Labs connector has no subscription, debit or payout tools (its tools are create_order, get_order_status, create_payment_link, initiate_refund, get_settlement_report and get_payout_analytics). That's why those parts use our mock, as the rules allow.

## MCP connectors on our server (Register Connector → MCP toggle on)

| Connector | MCP URL | Real or mock | Tools |
|---|---|---|---|
| **Pine Labs (Pacto)** | `https://<your-mock>/pinelabs/mcp` | Mock of Pine Labs' subscription and payout APIs, plus **NEW** guardian tools | create_plan, create_subscription, get_subscription, create_presentation, get_presentation_by_merchant_reference, send_subscription_notification, create_debit, create_merchant_retry, delete_presentation, resume_subscription, verify_upi_id, create_payout, get_payouts; **NEW:** add_guardian, request_guardian_decision, get_guardian_requests, record_guardian_decision, use_emergency_code |
| **Delhivery (Pacto)** | `https://<your-mock>/delhivery/mcp` | Mock of Delhivery's documented APIs, plus **NEW** presence check | check_pincode_serviceability, create_shipment, track_shipment; **NEW:** check_presence |
| **Gnani (Pacto)** | `https://<your-mock>/gnani/mcp` | **Real Gnani** speech-to-text and text-to-speech, plus **NEW** wellbeing signal | transcribe_voice_note, speak (real Gnani API with your key); **NEW:** wellbeing_signal |

If the platform can't connect to `/mcp`, use the SSE versions: `https://<your-mock>/pinelabs-sse/sse`, `/delhivery-sse/sse` and `/gnani-sse/sse`.

Every mock tool returns the partner API's response exactly as its REST endpoint returns it, including failures (low balance, timeout, malformed reply, pincode not serviceable, no rider, duplicate order, wrong UPI ID).

## The three capabilities that don't exist today

| Partner | Endpoint | What it does | Data the partner already holds that makes it possible |
|---|---|---|---|
| **Pine Labs** | `POST /ps/api/v1/public/subscriptions/{id}/guardian` (+ `/guardian/requests`, `/decision`, `/override`) | The Insurer holds authority over pausing or cancelling the mandate; the user can only ask, and a few emergency codes let them overturn an unfair refusal | The mandate, the payer's UPI identity, and Grantex's delegated-authority records. Pine Labs already supports non-revocable UPI Autopay mandates; only letting a named third party lift the lock is missing. |
| **Delhivery** | `POST /api/v1/geofence/presence` | From real GPS check-ins, decides whether someone arrived at a place and stayed | Geocoded addresses with error radii from its Maps APIs, and the geofencing it already runs to confirm rider arrivals |
| **Gnani** | `POST /api/v1/insights/wellbeing` | Says whether a speaker sounds like they're struggling, so the agent eases off | Over 14 million hours of Indian-language speech, including code-mixed Hinglish |

## Server settings (environment variables on Render)

- `PUBLIC_BASE_URL`: your server's public URL, e.g. `https://pacto-mock.onrender.com`. Needed so Gnani voice replies get a working link.
- `GNANI_API_KEY`: your real Gnani key. Also `GNANI_ORG_ID` and `GNANI_USER_ID` if your Gnani console shows them. If Gnani's console lists different header names, set `GNANI_EXTRA_HEADERS` as JSON, e.g. `{"X-API-Key-ID": "..."}`.
- `GNANI_STT_URL` and `GNANI_TTS_URL`: only if your Gnani console shows different endpoints from `https://api.vachana.ai/stt/v3` and `https://api.vachana.ai/api/v1/tts/sse`.
- `SLACK_BOT_TOKEN`: so the Gnani connector can download Slack voice clips (Slack file links need the bot token).
