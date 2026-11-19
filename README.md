# bKash Send Money Payment Page (auto SMS verify)

Customer tomar bKash number-e **Send Money** kore, tarpor page-e nijer **number + TrxID** dey.
Tomar phone-e bKash-er "You have received Tk ..." SMS ashle ta server-e pathiye deya hoy,
ar server TrxID + number + amount mile gele order automatically **paid** kore dey.

```
Customer --Send Money--> tomar bKash number
Customer --number + TrxID--> /pay/<order>        (page)
Tomar phone: bKash SMS --SMS forwarder app--> /webhook/sms   (auto)
Server: number + TrxID + amount mile gele -> PAID
```

SMS ager-e ashle ba pore ashle — duto-i kaj kore. Customer age submit korle page "verify hochhe..."
dekhay ar SMS ashar sathe sathe paid hoye jay.

## Files

| File | Kaj |
|---|---|
| `app.py` | Pura backend (Flask + SQLite) |
| `templates/pay.html` | Customer-er payment page (Bangla, mobile-first) |
| `templates/admin.html` | Admin: link banano, orders, SMS inbox, SMS log |
| `test_flow.py` | Fake SMS diye 38-ta test (`python3 test_flow.py`) |

## 1. Setup

```bash
pip install -r requirements.txt

export SHOP_NAME="Amar Dokan"
export RECEIVE_NUMBER="01XXXXXXXXX"      # tomar bKash number (jekhane taka ashbe)
export WEBHOOK_SECRET="lomba-random-string"   # SMS forwarder-ke eta dibe
export ADMIN_PASS="shokto-password"       # /admin login (user: admin)
export API_KEY="onno-random-string"       # optional: nijer website theke order banate

gunicorn -w 1 -b 127.0.0.1:8000 app:app   # production
# ba local test: python3 app.py  ->  http://127.0.0.1:5000/admin
```

`WEBHOOK_SECRET` / `ADMIN_PASS` / `API_KEY` set na korle oi feature **bondho** thake (insecure default nei).
Production-e **HTTPS must** (hosting-er SSL / Cloudflare / nginx). Proxy-r pichone chalale `TRUST_PROXY=1` dao.

> `-w 1` (ek worker) rakho: rate-limit memory-te thake. SQLite ek worker-e shobcheye nirapod.

## 2. Phone-e SMS forwarder

Tomar bKash number-er SIM ja phone-e ache, oi Android phone-e ekta **SMS forwarder** app lagbe
jeta incoming SMS HTTP POST korte pare (Play Store-e "SMS forwarder" / "SMS to URL" dhoroner app ache).
Setting:

- **URL:** `https://tomar-domain.com/webhook/sms`
- **Method:** POST, JSON
- **Header:** `X-Webhook-Secret: <WEBHOOK_SECRET>`  (header dite na parle URL-e `?key=<WEBHOOK_SECRET>`)
- **Body:**
  ```json
  {"from": "<sender>", "message": "<message text>"}
  ```
  (`<sender>` / `<message text>` er jaygay app-er nijossho placeholder boshate hobe — app-er help dekho.
  Server `from/sender/address` ar `message/body/text/content` — ei nam gulo bujhte pare.)
- Shudhu **bKash** theke asha SMS forward korar filter dile bhalo.
- Phone-er battery optimization oi app-er jonno **off** koro, ar phone-e internet chalu rakho.

**Sender name:** phone-er SMS inbox-e bKash-er SMS-er sender dekhao `bKash` hote hobe. Onno kichu hole
`ALLOWED_SENDERS="bKash,AnotherName"` set koro. Ei check-ta dorkar — noile je keu normal number
theke fake "You have received Tk..." SMS pathiye payment verify kore felte parto.

## 3. Use kora

1. `/admin` e jao -> **Create payment link** -> amount dao -> link customer-ke pathao.
   (Nijer website theke: `POST /api/orders` + header `X-API-Key`, body `{"amount": 500, "note": "Order #12"}` -> `{"ref","pay_url"}` pabe.)
2. Customer link-e Send Money kore number + TrxID dey -> auto verify.
3. `/admin` e shob order, asha SMS, ar ignore hoye jawa SMS dekhte paro.
4. SMS na ele (kintu bKash app-e taka dekhcho) -> order-er pashe **Mark paid** (manual).

## Ki ki check hoy

- TrxID **ekbarই** use hoy (database-e unique) — ek payment diye duto order paid hobe na.
- Customer-er deya number == SMS-e "from" number.
- SMS-er amount >= order amount (beshi pathale accept, kom hole reject).
- SMS sender `bKash` hote hobe.
- Ek IP theke 10 minute-e max 8 chesta (`MAX_ATTEMPTS`).
- Admin POST-e cross-site request block.

## Jeta tomake nijer SMS diye mil check korte hobe

- `app.py`-er SMS parser ei format dhore likha:
  `You have received Tk 500.00 from 01712345678. Fee Tk 0.00. Balance Tk ... TrxID 9BK1A2B3C4 at ...`
  Real bKash SMS-er format ektu alada hole **/admin -> SMS log** e "unrecognised" dekhabe —
  oi text amake dile parser thik kore debo (`SMS_AMOUNT_FROM` / `SMS_TRXID` regex).
- Ekta chhoto amount (jemon ৳10) nijer arek number theke pathiye pura flow ekbar test koro.

## Sheemabodhdhota

- Eta SMS-er upor nirbhor: phone off/offline thakle ba forwarder app bondho hole verify atke thakbe
  (order "pending" thakbe, SMS ashle auto hobe; nahole Mark paid).
- Sender ID spoofing kothin kintu ek-dom impossible na. Boro amounter jonno bKash app-e balance-o ekbar dekhe nao.
- Personal bKash number-e business-er beshi transaction hole bKash limit/restriction dite pare.
  Order barle official bKash merchant / Payment Gateway nite hote pare.
