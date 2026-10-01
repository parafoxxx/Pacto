# Pacto, Round 3 build kit (AgenticOrg)

Everything needed to build Pacto on Pine Labs' AgenticOrg platform, test it, record it and answer the questions. Submissions close **Sunday 4 October, 11:59 PM IST**.

```
mock-server/   FastAPI service: Pine Labs + Delhivery mocks, 3 NEW capabilities,
               3 MCP connectors (/pinelabs/mcp, /delhivery/mcp, /gnani/mcp),
               the Pacto check-in page (/checkin/<user>) and test controls (/_mock/*)
agent/         system_prompt_v1.md, system_prompt_v2.md (current), connectors.md, schedule_task.md
knowledge/     pacto_pact_book.md, for the Knowledge Base
evals/         eval_cases.md (10 cases), run_log.csv
submission/    answers_draft.md, decision_log.csv
```

## 1. Deploy the server (about 20 minutes)

1. Push this folder to GitHub.
2. On render.com, create a **Web Service** from the repo, with root directory `mock-server`. `render.yaml` sets the build and start commands.
3. Add the environment variables listed at the end of `agent/connectors.md`. At minimum: `PUBLIC_BASE_URL` and `GNANI_API_KEY`.
4. Check it: open `/docs`; run `python smoke_test.py https://<your-mock>.onrender.com`; open `/checkin/rahul` on your phone and tap Check in.

Use Render, not Vercel: the mock keeps state in memory, and Vercel restarts too often. Free Render services sleep when idle, so open `/docs` a minute before recording, then call `POST /_mock/reset`.

## 2. Set up AgenticOrg

1. Sign in, choose **Requested role: Developer** and **Business domain: Back Office**, and join the org **"Ken's case competition"**. (If you see error 429, wait about 30 minutes.)
2. **Register Connector** three times with the MCP toggle **on**: base URLs `https://<your-mock>/pinelabs/mcp`, `/delhivery/mcp` and `/gnani/mcp`. Run the connection test and health check on each.
3. Connect the native **Slack, WhatsApp, Gmail, Google Calendar** and **Pine Labs Plural** connectors.
4. **Knowledge Base:** upload `knowledge/pacto_pact_book.md` after filling in real values.

## 3. Real-world setup (about 30 minutes)

- **Slack:** a free workspace "Pacto". Teammate 1 is Rahul (the user), teammate 2 is Priya (the Insurer).
- **WhatsApp:** the circle number, which can be teammate 3's phone.
- **Google Calendar:** an event titled `Pacto gym: rahul` for each session you'll test.
- **Pine Labs subscription for Rahul:** create it once, either from the server's `/docs` page (create_plan → create_subscription → add_guardian) or by asking the agent in chat. Paste the subscription_id into the pact book.

## 4. Build the agent

Create the agent with the wizard: Persona "Pacto", domain Back Office, type Custom Agent. Paste `agent/system_prompt_v2.md` as the prompt. Then:
- **Authorized tools:** tick only the tools listed under "Use" in the prompt.
- **Knowledge base:** add the pact book through **Add Tool**.
- **Confidence threshold:** around 0.8 to start. Anything the model is unsure about goes to the Approvals tab; note those in the run log.
- **Schedule:** **My Schedule → every 5 minutes**, with the text from `agent/schedule_task.md`.

The agent starts as a **Shadow Agent**. Test it in chat first.

## 5. Test in rounds (Saturday)

1. Run the 10 cases in `evals/eval_cases.md`, queuing each failure on the server before the case (`POST /_mock/scenario`).
2. For each run, use **"Why did the agent do this?"** to read its reasoning, and fill a row in `evals/run_log.csv`.
3. After each round, edit the prompt in the platform (its edit history records every version) and save a copy as `system_prompt_v3.md`, v4 and so on, with a one-line change note at the top.
4. Export `GET /_mock/log` after each round: it lists every call the agent made to the mock, with IST timestamps.

For the recording, shorten the notice window with `POST /_mock/settings {"notice_seconds": 120}` and say so in the video; the real rail requires at least 24 hours.

## 6. Record (Sunday morning)

Keep the AgenticOrg screen and Slack visible together the whole time.
- **Run A, main story:** Rahul taps Check in and leaves early → the scheduled run finds TOO_SHORT → asks on Slack → Rahul sends a voice clip "yaar bukhaar aa gaya tha" → Gnani transcribes it → guardian pause request → Priya replies APPROVE → paused → Gnani voice reply.
- **Run B, a different human input:** Priya replies REJECT → Rahul says "hmm" (Pacto asks again) → "haan" → emergency code used → the circle is told on WhatsApp.
- **Run C, a different human input:** Rahul doesn't reply → debit after the notice window (queue `low_balance` ×4 to reach HALTED) → **real Pine Labs payment link** on Slack → paid in UAT → payout to Priya → Gmail receipt → WhatsApp to the circle.
- **Optional Run D:** Rahul redeems coins → pincode check → shipment → tracking.

## 7. Answer the questions (Sunday afternoon)

`submission/answers_draft.md`. The decision list comes from the run history, "Why did the agent do this?" and `/_mock/log`.
