# Bank Transaction Monitoring (Odoo 17)

This is an Odoo add-on (called a **module**) that watches a bank's transactions for it, automatically. Nobody has to press a button.

Every 5 minutes it checks the new transactions and:

| The bank asked for… | What this module does |
|---|---|
| Flag transactions of ₦10 million or more | Marks them **Flagged for Review** with the reason "High value" |
| Spot customers who make more than 5 transactions in 10 minutes | Marks the transaction that crosses the limit **Flagged for Review** with the reason "High frequency" |
| Handle normal transactions, but never twice | Marks them **Processed**, once only |
| Cope with errors and very large numbers of transactions | Works in groups of 1,000, saves after each group, and puts any broken transaction aside as **Failed** so the rest carry on |

---

## 1. How it works, in plain words

Imagine the bank has a **post room**.

1. **Letters arrive.** Each new transaction drops into an "incoming" tray with the status **Pending**. It can be imported, sent by another system, or typed in by hand.
2. **A clerk comes round every 5 minutes.** This clerk is a background job built into Odoo (a *scheduled action*). It takes the oldest 1,000 letters from the tray.
3. **The clerk sorts each letter onto a pile:**
   - **Processed**: nothing unusual.
   - **Flagged for Review**: the amount is ₦10M or more, the customer is transacting too fast, or both. The reason is written on it.
   - **Failed**: something went wrong while checking it. The error is written on it, and someone can press **Retry** later.
4. **The clerk saves the work** after every 1,000, then takes the next 1,000, until the tray is empty.
5. **A supervisor reviews the flagged pile.** A compliance officer opens **Transaction Monitoring → Review Queue**, reads why each one was flagged, adds a note, and presses **Approve** or **Reject**. The module remembers who decided and when.

```
                              nothing unusual   ┌───────────┐
                         ┌───────────────────►  │ Processed │
                         │                      └───────────┘
 new ──► ┌───────────┐   │  a rule is broken    ┌───────────┐  Approve  ┌──────────┐
         │  Pending  │ ──┼───────────────────►  │  Flagged  │ ────────► │ Approved │
         └───────────┘   │                      └───────────┘ ────────► │ Rejected │
               ▲         │  error               ┌───────────┐  Reject   └──────────┘
               │         └───────────────────►  │  Failed   │
               │                                └─────┬─────┘
               └──────────────── Retry ───────────────┘
```

---

## 2. The two rules

### Rule 1: big amounts

Any transaction of **₦10,000,000 or more** is flagged. "Or more" means exactly ₦10,000,000 counts too.

| Amount | Result |
|---|---|
| ₦9,999,999.99 | Processed |
| ₦10,000,000.00 | Flagged |
| ₦25,000,000.00 | Flagged |

### Rule 2: too many transactions too fast

For each transaction, the module looks back **10 minutes** and counts how many transactions **that same customer** made, including this one. If the count is **more than 5**, the transaction is flagged.

Example for one customer, Ada:

| Time | Ada's transactions in the 10 minutes up to this one | Result |
|---|---|---|
| 10:00 | 1 | Processed |
| 10:01 | 2 | Processed |
| 10:02 | 3 | Processed |
| 10:03 | 4 | Processed |
| 10:04 | 5 | Processed (5 is allowed) |
| 10:09 | **6** | **Flagged** |

Notes:
- **Why only the 6th is flagged:** five transactions are allowed, so the first five did nothing wrong. The 6th is the one that crosses the line.
- Each customer is counted **separately**. Two customers never add to each other's count.
- Transactions checked in an **earlier run still count**. If five were handled at 10:05 and the sixth arrives at 10:10, it is still caught.
- Flagging **does not stop the money**. It just asks a person to look at it.

---

## 3. What's in the project

```
addons/bank_tx_monitoring/
├── __manifest__.py                      the module's "ID card": name, version, files to load
├── models/bank_transaction.py           the main code: what a transaction is, the rules, the background job
├── data/ir_cron_data.xml                sets up the job that runs every 5 minutes
├── data/ir_config_parameter_data.xml    the settings (₦10M limit, 5 transactions, 10 minutes, etc.)
├── security/ir.model.access.csv         who is allowed to see, change and delete transactions
├── views/bank_transaction_views.xml     the screens: list, form, search filters and menus
└── tests/test_transaction_monitoring.py automatic tests that prove each rule works
docker-compose.yml                       starts Odoo and its database with one command
requirements.txt                         extra Python packages needed (none)
```

---

## 4. How to run it

### Step 1: install Docker
Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop/). It runs Odoo and its database for you, so you don't have to install them yourself.

### Step 2: start Odoo
Open a terminal in this folder and run:

```bash
docker compose up -d
```

### Step 3: install the module
1. Open http://localhost:8069 in your browser.
2. Create a database. Any name works; remember the email and password you choose.
3. Go to **Apps**, clear the "Apps" filter in the search bar, search for **Bank Transaction Monitoring**, and click **Install**.

### Step 4: try it
1. Go to **Transaction Monitoring → All Transactions** and create:
   - one transaction of ₦12,000,000,
   - six transactions for the same customer, one minute apart,
   - a few normal ones.
2. Click **Run Monitoring Now** at the top of the list, or wait up to 5 minutes for the job.
3. Refresh the page. The big one and the 6th fast one turn **red** (Flagged); the others are **Processed**.

### Run the automatic tests

```bash
docker compose run --rm odoo odoo -d test_db -i bank_tx_monitoring \
    --test-enable --test-tags /bank_tx_monitoring --stop-after-init
```

If everything is fine, the output ends without any lines containing `FAIL` or `ERROR`.

### Optional: test with 100,000 transactions

Open the Odoo shell (a Python prompt connected to your database):

```bash
docker compose run --rm odoo odoo shell -d <your_database_name>
```

Then paste this. It creates 2,000 customers and 100,000 random transactions, then runs the monitoring job once:

```python
from datetime import datetime, timedelta
import random
partners = env["res.partner"].create([{"name": f"Customer {i}"} for i in range(2000)])
start = datetime.now() - timedelta(days=1)
for chunk in range(100):
    env["bank.transaction"].create([{
        "name": f"LOAD-{chunk:03d}-{i:04d}",
        "partner_id": random.choice(partners).id,
        "amount": random.choice([5_000, 50_000, 250_000, 15_000_000]),
        "transaction_date": start + timedelta(seconds=chunk * 1000 + i),
    } for i in range(1000)])
env.cr.commit()
env["bank.transaction"]._cron_monitor_transactions()
```

---

## 5. Design decisions, explained simply

### Keeping the data simple
- **One table of transactions** (the model `bank.transaction`) with a **status** field: Pending, Processed, Flagged, Failed, Approved or Rejected. The job only has to ask for "Pending" to find its work.
- **Customers are Odoo's normal contacts** (`res.partner`), so there's no second customer list to keep in sync.
- **Two yes/no boxes**, "High value" and "High frequency", plus a written reason. This makes it easy to filter, for example "show me all high-frequency alerts this week".
- **Review details** (who reviewed it, when, and their note) are saved on the transaction.

### Running in the background
- The job is an Odoo **scheduled action** (`ir.cron`) that runs every 5 minutes. Users never wait for it.
- Odoo stops any job that runs for too long. So this job **stops itself after 60 seconds** and asks Odoo to start it again straight away. A huge backlog gets worked through in safe slices.
- The **Run Monitoring Now** button starts the same background job immediately. It doesn't freeze your screen.

### Never handling a transaction twice
There are four layers of protection:
1. **No duplicate references.** The database refuses to store two transactions with the same bank reference, so a file sent twice can't create duplicates.
2. **Only Pending transactions are picked up.** Once checked, a transaction is never picked up again.
3. **"Do not disturb" signs.** When the job takes a group of transactions it locks them (`FOR UPDATE SKIP LOCKED`). If a second copy of the job runs at the same time, it skips the locked ones and takes different ones.
4. **All or nothing.** A group's results are saved in one go. If the server crashes halfway through, that group goes back to Pending and is checked again next time. Nothing is ever half done.

### When something goes wrong
- **Undo points.** Each group of 1,000 is checked inside a database *savepoint*. If something fails, only that group is undone.
- **Find the bad one.** The job then re-checks that group one transaction at a time. The good ones are processed. Only the broken one is marked **Failed**, with the error message. One bad transaction never blocks the other 999.
- **Retry button.** Once the problem is fixed, press **Retry** and the transaction goes back into the queue.
- **History can't be changed.** After a transaction is checked, its reference, customer, amount, currency and date can't be edited, and it can't be deleted. No one can quietly lower an amount after it was flagged.
- **The database guards the data too.** Amounts must be above zero, and references must be unique. These rules hold even for data that doesn't come through the screens.
- **Mistakes in settings don't break it.** If someone types "ten million" instead of 10000000, the job writes a warning in the log and uses the default.
- **Everything is logged.** Each run records how many transactions it handled. Each failure is recorded in full for IT.

### Staying fast with millions of transactions
- **Groups of 1,000, not one at a time.** Odoo loads the whole group's data in a few trips to the database instead of one per transaction.
- **One question instead of 1,000.** For the 10-minute rule, one database query counts recent transactions for the whole group, instead of one query per transaction.
- **One save for all normal transactions.** Only the rare flagged ones are saved one by one, because each has its own reason.
- **Indexes, like the index at the back of a book.** They let the database jump straight to the right rows:
  - one for "this customer's transactions around this time" (used by the 10-minute rule),
  - one that only lists **Pending** transactions, so finding the next 1,000 stays instant however much history builds up.
- **No chat history on each transaction.** Odoo can log every status change as a message, but at this volume that would slow things down a lot. The review fields keep the audit trail instead.

---

## 6. Words you'll see

| Word | Meaning |
|---|---|
| **Module** | An add-on that gives Odoo a new feature. This project is one module. |
| **Model** | A type of record Odoo stores, like "Bank Transaction". Odoo turns it into a database table. |
| **Field** | One piece of information on a record, like the amount. |
| **ORM** | Odoo's tool that lets the code work with records as Python objects instead of writing database queries by hand. |
| **Scheduled action / cron** | A task Odoo runs automatically on a timer. |
| **Commit** | Saving changes permanently to the database. |
| **Savepoint** | An "undo point". If something fails after it, only the work since that point is undone. |
| **Lock** | A "do not disturb" sign on a row, so two processes don't change it at once. |
| **Index** | A shortcut that lets the database find rows without reading the whole table. |
