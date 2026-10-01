"""Quick check that a deployed mock works end to end.
Usage: python smoke_test.py https://your-mock.onrender.com
"""
import sys, json, urllib.parse, httpx

base = sys.argv[1].rstrip("/")
c = httpx.Client(base_url=base, timeout=30)
c.post("/_mock/reset"); c.post("/_mock/settings", json={"notice_seconds": 0})
tok = c.post("/api/auth/v1/token", json={"client_id": "pacto", "client_secret": "demo"}).json()["access_token"]
H, D = {"Authorization": f"Bearer {tok}"}, {"Authorization": "Token demo"}
plan = c.post("/ps/api/v1/public/plans", headers=H, json={"plan_name": "Pacto stake", "frequency": "AS",
       "amount": {"value": 20000, "currency": "INR"}, "max_limit_amount": {"value": 100000, "currency": "INR"}}).json()
sub = c.post("/ps/api/v1/public/subscriptions", headers=H, json={"plan_id": plan["plan_id"],
      "customer": {"name": "Rahul", "vpa": "rahul@okaxis"}, "non_revocable": True}).json()
sid = sub["subscription_id"]
pre = c.post(f"/ps/api/v1/public/subscriptions/{sid}/presentations", headers=H,
      json={"amount": {"value": 20000, "currency": "INR"}, "merchant_presentation_reference": "smoke-1"}).json()
c.post("/ps/api/v1/public/subscriptions/notify", headers=H, json={"presentation_id": pre["presentation_id"]})
print("debit:", c.post("/ps/api/v1/public/subscriptions/execute", headers=H, json={"presentation_id": pre["presentation_id"]}).json()["status"])
print("payout:", c.post("/payouts/v3/payments/banks", headers=H, json={"clientReferenceId": "smoke-1", "payeeName": "Priya",
      "amount": {"value": 20000, "currency": "INR"}, "mode": "UPI", "vpa": "priya@okhdfc"}).json()["status"])
print("pincode:", c.get("/c/api/pin-codes/json/?filter_codes=208016", headers=D).json()["delivery_codes"][0]["postal_code"]["pre_paid"])
data = {"shipments": [{"name": "Rahul", "add": "IIT Kanpur", "pin": "208016", "phone": "9000000000", "order": "SMOKE-1",
        "payment_mode": "Prepaid", "hsn_code": "21061000", "seller_gst_tin": "09ABCDE1234F1Z5"}], "pickup_location": {"name": "PACTO-PROTEIN-WH"}}
print("order:", c.post("/api/cmu/create.json", headers=D, content="format=json&data=" + urllib.parse.quote_plus(json.dumps(data))).json()["success"])
print("all good" if True else "")
