{
    "name": "Bank Transaction Monitoring",
    "version": "17.0.1.0.0",
    "summary": "Background monitoring of bank transactions: high-value and high-frequency alerts",
    "description": """
Automated transaction monitoring for a bank.

A scheduled action (cron) runs every few minutes and, for every new transaction:
  * flags it for review when the amount is NGN 10,000,000 or more,
  * flags it for review when the customer made more than 5 transactions in 10 minutes,
  * otherwise marks it as processed.

Transactions are processed in batches, exactly once, and one bad record never
stops the rest of the run.
    """,
    "author": "neetoosan (Israel Oyekanmi)",
    "category": "Accounting/Banking",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "data/ir_cron_data.xml",
        "views/bank_transaction_views.xml",
    ],
    "installable": True,
    "application": True,
}
