# Pacto, Round 3 answers (draft)

Anything in [brackets] gets filled in from the recording and test logs. Rewrite the story in your own words.

## Part 1: Your agent

### The story of one person (100 words max)

[Draft, fill in the real date and times from Run A]

On [Saturday 3 October], Rahul checks in at his gym at 7:02 PM but leaves by 7:10. At 8 PM, Pacto messages him on Slack: "Aaj ka session record nahi hua. Kya hua?" Rahul sends a voice clip: "Yaar, bukhaar aa gaya tha." Pacto understands him through Gnani and asks his sister Priya, his Insurer, to approve a pause. Priya taps APPROVE. Pacto pauses his stake, deletes the pending ₹200 charge, and sends Rahul a voice note: "Priya ne pause approve kar diya. Is hafte koi charge nahi. Get well soon." A genuine sick day costs him nothing.

### Every decision, in order

Fill from the Decisions tab and `/_mock/log`, one row per decision, using `decision_log.csv`. Example of the shape:

| When | Received | From | Decided | Why | Did or said, to whom | Through |
|---|---|---|---|---|---|---|
| [3 Oct, 20:05:04] | Calendar event "Pacto gym: rahul" 19:00–20:00 ended; check-in at 19:02, check-out at 19:10 | Google Calendar (real); Rahul's phone GPS via the Pacto check-in page | Not present (TOO_SHORT) | R2, R3 | To Rahul: "Saturday 7 PM ka session record nahi hua. Kya hua?" | Delhivery check_presence (mock, new); Slack (real) |
| [20:10:41] | Voice clip, transcribed: "yaar bukhaar aa gaya tha" | Slack (real) → Gnani speech-to-text (real) | ILL; wellbeing OK | R5 | Guardian PAUSE request; to Priya: "Rahul ne 3 Oct ke session ke liye pause maanga hai (bukhaar). Reply APPROVE or REJECT" | Pine Labs request_guardian_decision (mock, new); Slack (real) |
| [20:15:12] | "APPROVE" from Priya's own Slack user | Slack (real) | Pause approved | R6 | record_guardian_decision → subscription PAUSED; to Rahul: "Priya ne pause approve kar diya. Is hafte koi charge nahi." plus a voice reply | Pine Labs (mock, new); Gnani speak (real); Slack (real) |

### Every connector

| Connector | Real or mock | Used for |
|---|---|---|
| Gnani speech-to-text and text-to-speech (through our MCP connector) | Real Gnani API | Understanding voice clips; speaking key replies |
| Pine Labs Plural (native) | Real, Pine Labs UAT | Payment link when Autopay fails; checking it was paid; refund if a charge is overturned |
| Pine Labs subscriptions and payouts (our MCP connector) | Mock | Charge request, pre-debit notice, debit, retries, payout to the Insurer, UPI ID check |
| Delhivery (our MCP connector) | Mock | Pincode check, shipment and tracking for protein bought with coins |
| Slack | Real | Conversations with the user and the Insurer |
| WhatsApp | Real | Notices to the user's circle |
| Gmail | Real | Receipts when a stake is charged |
| Google Calendar | Real | The user's gym sessions |
| Pacto check-in page | Real phone GPS | Check in and check out at the gym |

### The three capabilities that don't exist today

1. **Pine Labs: guardian-held authority over a mandate.** `POST /ps/api/v1/public/subscriptions/{id}/guardian`, with `/guardian/requests`, `/decision` and `/override`. The Insurer, not the payer, decides whether a pause or cancellation goes through, and a limited number of override codes protect the payer from an unfair refusal. **Data Pine Labs already holds:** the mandate, the payer's UPI identity and Grantex's delegated-authority records, and it already offers non-revocable UPI Autopay mandates. The missing piece is letting a named third party lift the lock.
2. **Delhivery: presence at a place.** `POST /api/v1/geofence/presence`. Given a verified place and timestamped pings, it says whether someone arrived and stayed. **Data Delhivery already holds:** geocoded addresses with error radii from its Maps APIs, and the geofencing it runs to confirm rider arrivals.
3. **Gnani: wellbeing signal.** `POST /api/v1/insights/wellbeing`. It says whether a speaker sounds like they're struggling, so the agent eases off instead of pushing harder. **Data Gnani already holds:** over 14 million hours of Indian-language speech, including code-mixed Hinglish.

### How agent-ready each rail is (draft scores, adjust after building)

- **Pine Labs: [8]/10.** An official MCP server already exposes the whole subscription lifecycle (plan, presentation, pre-debit notification, debit, merchant retry), so an agent can run a stake end to end. It loses points because payouts aren't in the MCP tools, and no third party can hold authority over a mandate.
- **Gnani: [6]/10.** Its Indian-language speech-to-text and text-to-speech are strong, including Hinglish. But in Round 2, its agent test calls reached only whitelisted numbers, a live action arrived with an empty body, and it returns no sentiment. [Update with what you see in this round.]
- **Delhivery: [5]/10.** Its shipping APIs cover what an agent needs (pincode check, order creation, tracking, push updates), but they use static token auth and a form-encoded `format=json&data=` payload, the public docs were last updated years ago, tracking is rate-limited to 750 requests per 5 minutes per IP, and only Maps, not shipping, has an MCP server. [Update with what you see.]

## Part 2: How you got your agent here

### The 10 eval cases
See `evals/eval_cases.md`.

### Run log for every round, and what changed
Export `evals/run_log.csv` and `/_mock/log` after each round. Write one line per prompt change: what failed → what you changed → the result next round.

### Final system prompt and every version
`agent/system_prompt_v1.md`, then v2, v3… Put the change note at the top of each.

### Cases the agent still fails, and why
[Fill after testing. Likely candidates to check honestly:]
- **Timing:** the pre-debit notice is shortened to about 2 minutes on our mock for the recording; the real rail requires at least 24 hours.
- **Voice notes in noisy places** or long rambling ones, if Gnani's transcript misses the key word.
- **[Anything from your final test round that still fails.]**
