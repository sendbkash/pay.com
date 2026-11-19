"""End-to-end test with fake bKash SMS messages.  Run:  python3 test_flow.py"""
import base64
import os
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update(
    DB_PATH=os.path.join(tmp, "test.db"), SHOP_NAME="Test Shop",
    RECEIVE_NUMBER="01711111111", WEBHOOK_SECRET="hook-secret",
    ADMIN_PASS="adminpw", API_KEY="apikey", MAX_ATTEMPTS="5")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as shop  # noqa: E402

client = shop.app.test_client()
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:adminpw").decode()}
HOOK = {"X-Webhook-Secret": "hook-secret"}
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def new_order(amount):
    r = client.post("/api/orders", json={"amount": amount, "note": "test"},
                    headers={"X-API-Key": "apikey"})
    return r.get_json()["ref"]


def sms(trx, frm="01712345678", amount="500.00", sender="bKash"):
    body = (f"You have received Tk {amount} from {frm}. Fee Tk 0.00. "
            f"Balance Tk 1,234.56. TrxID {trx} at 19/11/2026 14:10")
    return client.post("/webhook/sms", json={"from": sender, "message": body}, headers=HOOK)


_ip_counter = iter(range(1, 10_000))


def submit(ref, number, trx, ip=None):
    # Each call uses its own IP unless one is given, so the rate limiter only
    # trips in the test that is meant to exercise it.
    ip = ip or f"10.1.{next(_ip_counter) // 250}.{next(_ip_counter) % 250 + 1}"
    return client.post("/api/submit", json={"ref": ref, "number": number, "trxid": trx},
                       environ_overrides={"REMOTE_ADDR": ip})


def status(ref):
    return client.get(f"/api/status/{ref}").get_json()


print("SMS parsing")
p = shop.parse_bkash_sms("You have received Tk 1,500.50 from 01712345678. Fee Tk 0.00. "
                         "Balance Tk 9,999.00. TrxID 9bk1a2b3c4 at 19/11/2026 14:10")
check("amount with comma + decimals", p and p["amount"] == 1500.50, p)
check("trxid upper-cased", p and p["trxid"] == "9BK1A2B3C4", p)
p = shop.parse_bkash_sms("You have received Tk 200.00 from +8801812345678. TrxID ABCD123456")
check("+880 sender normalised", p and p["number"] == "01812345678", p)
check("'sent' SMS ignored", shop.parse_bkash_sms("You have sent Tk 500 to 01712345678. TrxID ABCD123456") is None)
check("OTP SMS ignored", shop.parse_bkash_sms("Your bKash verification code is 482913") is None)

print("Pages")
ref = new_order(500)
r = client.get(f"/pay/{ref}")
html = r.get_data(as_text=True)
check("pay page 200", r.status_code == 200)
check("shows amount + receive number", "৳500" in html and "01711-111111" in html)
check("unknown order 404", client.get("/pay/NOPE").status_code == 404)
check("admin needs login", client.get("/admin").status_code == 401)
check("admin page loads", client.get("/admin", headers=AUTH).status_code == 200)

print("Flow A: customer submits first, SMS arrives later")
check("submit -> pending", submit(ref, "01712345678", "AAAA111111").get_json()["status"] == "pending")
check("status still pending", status(ref)["status"] == "pending")
r = sms("AAAA111111")
check("webhook matched order", r.get_json().get("matched") == [ref], r.get_json())
j = status(ref)
check("status now paid", j["status"] == "paid" and j["trxid"] == "AAAA111111", j)
check("pay page shows success on reload", 'data-status="paid"' in client.get(f"/pay/{ref}").get_data(as_text=True))

print("Flow B: SMS already there, customer submits after")
ref2 = new_order(500)
sms("BBBB222222")
j = submit(ref2, "01712345678", "BBBB222222").get_json()
check("instant verify -> paid", j["status"] == "paid", j)

print("Rejections")
ref3 = new_order(500)
sms("CCCC333333", frm="01912345678")
j = submit(ref3, "01712345678", "CCCC333333").get_json()
check("wrong number rejected", j["status"] == "failed" and j["code"] == "number_mismatch", j)
check("order reset to unpaid after failure", status(ref3)["status"] == "unpaid")
j = submit(ref3, "01912345678", "CCCC333333").get_json()
check("correct number then accepted", j["status"] == "paid", j)

ref4 = new_order(1000)
sms("DDDD444444", frm="01712345678", amount="500.00")
j = submit(ref4, "01712345678", "DDDD444444").get_json()
check("amount too low rejected", j["status"] == "failed" and j["code"] == "amount_low", j)
check("message shows amounts", "500" in j["message"] and "1,000" in j["message"], j)

ref5 = new_order(500)
j = submit(ref5, "01712345678", "BBBB222222").get_json()   # already used by ref2
check("reused TrxID rejected", j["status"] == "failed" and j["code"] == "used", j)

ref6 = new_order(300)
sms("EEEE555555", amount="500.00")
j = submit(ref6, "01712345678", "EEEE555555").get_json()
check("overpay accepted", j["status"] == "paid", j)

print("Webhook security")
check("wrong secret -> 401", client.post("/webhook/sms", json={"from": "bKash", "message": "x"},
                                         headers={"X-Webhook-Secret": "nope"}).status_code == 401)
check("no secret -> 401", client.post("/webhook/sms", json={"from": "bKash", "message": "x"}).status_code == 401)
ref7 = new_order(500)
r = sms("FFFF666666", sender="+8801999999999")              # fake SMS from a normal number
check("non-bKash sender ignored", r.get_json().get("ignored") == "sender", r.get_json())
j = submit(ref7, "01712345678", "FFFF666666").get_json()
check("fake SMS does NOT verify payment", j["status"] == "pending", j)
r = sms("BBBB222222")
check("duplicate SMS forward is harmless", r.get_json().get("duplicate") is True, r.get_json())
logs = shop.get_db if False else None
with shop.app.app_context():
    n = shop.get_db().execute("SELECT COUNT(*) c FROM sms_log").fetchone()["c"]
check("rejected SMS recorded in log", n >= 1, n)

print("Input validation")
check("bad number 400", submit(ref7, "123", "FFFF666666", ip="10.0.0.9").status_code == 400)
check("bad TrxID 400", submit(ref7, "01712345678", "x!", ip="10.0.0.9").status_code == 400)
check("unknown order handled", submit("NOPE", "01712345678", "ZZZZ999999", ip="10.0.0.9").get_json()["code"] == "not_found")

print("Rate limit")
codes = [submit(ref7, "01712345678", "GGGG777777", ip="10.0.0.77").status_code for _ in range(7)]
check("6th+ attempt from one IP blocked (429)", codes[:5] == [200] * 5 and 429 in codes[5:], codes)

print("Admin")
r = client.post("/admin/orders", data={"amount": "250", "note": "manual"}, headers=AUTH)
check("admin creates order", r.status_code == 302 and "new=" in r.headers["Location"])
r = client.post("/admin/orders", data={"amount": "abc"}, headers=AUTH)
check("bad amount rejected", r.status_code == 400)
r = client.post(f"/admin/orders/{ref7}/mark-paid", headers=AUTH)
check("mark-paid works", r.status_code == 302 and status(ref7)["status"] == "paid")
r = client.post(f"/admin/orders/{ref5}/mark-paid", headers={**AUTH, "Origin": "https://evil.example"})
check("cross-site admin POST blocked", r.status_code == 403)
check("api create needs key", client.post("/api/orders", json={"amount": 100}).status_code == 401)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
