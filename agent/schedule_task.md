# Text for AgenticOrg "My Schedule" (recurring run every 5 minutes)

Run Pacto's session check. List Google Calendar events titled "Pacto gym: <user_id>" that ended in the last 24 hours. For each one, follow rules R1 to R11: check whether it was already handled, check presence, ask before calling it a miss, request the charge, read and act on Slack replies from the user and the Insurer, charge only after the notice window, retry failed charges, and pay the Insurer. Then read Slack for coin redemption requests and follow R12. Report every decision with its rule number.
