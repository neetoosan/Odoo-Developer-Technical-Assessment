import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, formatLang

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "bank_tx_monitoring."
DEFAULT_PARAMS = {
    "high_value_threshold": 10_000_000.0,
    "velocity_max_count": 5,
    "velocity_window_minutes": 10,
    "batch_size": 1000,
    "max_run_seconds": 60,
}

LOCKED_FIELDS = {"name", "partner_id", "amount", "currency_id", "transaction_date"}


class BankTransaction(models.Model):
    _name = "bank.transaction"
    _description = "Bank Transaction"
    _order = "transaction_date desc, id desc"

    name = fields.Char(
        string="Reference",
        required=True,
        index=True,
        copy=False,
        help="Unique reference given to the transaction by the core banking system.",
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        index=True,
        ondelete="restrict",
    )
    amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency",
        string="Currency",
        required=True,
        default=lambda self: self._default_currency(),
    )
    transaction_date = fields.Datetime(
        required=True,
        index=True,
        default=fields.Datetime.now,
        help="When the customer made the transaction (not when it was imported).",
    )
    description = fields.Char()

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processed", "Processed"),
            ("flagged", "Flagged for Review"),
            ("failed", "Failed"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        default="pending",
        required=True,
        index=True,
        copy=False,
    )

    is_high_value = fields.Boolean(string="High Value", readonly=True, copy=False)
    is_high_frequency = fields.Boolean(string="High Frequency", readonly=True, copy=False)
    flag_reason = fields.Text(readonly=True, copy=False)
    processed_on = fields.Datetime(readonly=True, copy=False)
    error_message = fields.Text(readonly=True, copy=False)

    reviewed_by_id = fields.Many2one("res.users", string="Reviewed By", readonly=True, copy=False)
    reviewed_on = fields.Datetime(readonly=True, copy=False)
    review_note = fields.Text()

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "A transaction with this reference already exists."),
        ("amount_positive", "CHECK(amount > 0)", "The transaction amount must be greater than zero."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS bank_transaction_partner_date_idx
                ON bank_transaction (partner_id, transaction_date);
            CREATE INDEX IF NOT EXISTS bank_transaction_pending_queue_idx
                ON bank_transaction (transaction_date, id)
                WHERE state = 'pending';
            """
        )

    @api.model
    def _default_currency(self):
        return self.env.ref("base.NGN", raise_if_not_found=False) or self.env.company.currency_id

    def write(self, vals):
        if LOCKED_FIELDS & set(vals) and any(tx.state != "pending" for tx in self):
            raise UserError(
                _("You cannot change the reference, customer, amount, currency or date of a transaction that has already been checked.")
            )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_pending(self):
        if any(tx.state != "pending" for tx in self):
            raise UserError(_("Only pending transactions can be deleted. Checked transactions are part of the audit trail."))

    def action_approve(self):
        self._set_review_outcome("approved")

    def action_reject(self):
        self._set_review_outcome("rejected")

    def _set_review_outcome(self, new_state):
        if any(tx.state != "flagged" for tx in self):
            raise UserError(_("Only flagged transactions can be approved or rejected."))
        self.write({
            "state": new_state,
            "reviewed_by_id": self.env.user.id,
            "reviewed_on": fields.Datetime.now(),
        })

    def action_retry(self):
        if any(tx.state != "failed" for tx in self):
            raise UserError(_("Only failed transactions can be retried."))
        self.write({"state": "pending", "error_message": False})

    def action_run_monitoring_now(self):
        self.env.ref("bank_tx_monitoring.ir_cron_monitor_transactions").sudo()._trigger()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Monitoring started"),
                "message": _("Pending transactions are being checked in the background. Refresh in a moment."),
                "type": "info",
                "sticky": False,
            },
        }

    @api.model
    def _cron_monitor_transactions(self):
        self._run_monitoring(auto_commit=True)

    @api.model
    def _run_monitoring(self, auto_commit=False):
        config = self._get_monitoring_config()
        started = time.monotonic()
        total = 0

        while True:
            batch = self._lock_next_batch(config["batch_size"])
            if not batch:
                break

            self._process_batch(batch, config)
            total += len(batch)
            if auto_commit:
                self.env.cr.commit()

            if time.monotonic() - started > config["max_run_seconds"]:
                _logger.info("Transaction monitoring: time budget used, rescheduling to continue.")
                self.env.ref("bank_tx_monitoring.ir_cron_monitor_transactions").sudo()._trigger()
                break

        _logger.info("Transaction monitoring: %s transaction(s) handled in %.1fs.", total, time.monotonic() - started)
        return total

    @api.model
    def _lock_next_batch(self, batch_size):
        self.flush_model(["state", "transaction_date"])
        self.env.cr.execute(
            """
            SELECT id
              FROM bank_transaction
             WHERE state = 'pending'
          ORDER BY transaction_date, id
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            [batch_size],
        )
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    @api.model
    def _process_batch(self, batch, config):
        try:
            with self.env.cr.savepoint():
                batch._apply_monitoring_rules(config)
            return
        except Exception:
            _logger.warning("Transaction monitoring: batch of %s failed, retrying one by one.", len(batch), exc_info=True)
            self.env.invalidate_all(flush=False)

        for tx in batch:
            try:
                with self.env.cr.savepoint():
                    tx._apply_monitoring_rules(config)
            except Exception as error:
                self.env.invalidate_all(flush=False)
                _logger.exception("Transaction monitoring: transaction %s failed.", tx.id)
                tx.write({
                    "state": "failed",
                    "error_message": str(error) or error.__class__.__name__,
                    "processed_on": fields.Datetime.now(),
                })

    def _apply_monitoring_rules(self, config):
        now = fields.Datetime.now()
        recent_counts = self._count_recent_transactions(config["velocity_window_minutes"])

        normal_ids = []
        for tx in self:
            flag_values = tx._evaluate_transaction(config, recent_counts.get(tx.id, 1))
            if flag_values:
                tx.write(dict(flag_values, state="flagged", processed_on=now, error_message=False))
            else:
                normal_ids.append(tx.id)

        if normal_ids:
            self.browse(normal_ids).write({"state": "processed", "processed_on": now, "error_message": False})

    def _evaluate_transaction(self, config, recent_count):
        self.ensure_one()
        reasons = []

        ngn = self.env.ref("base.NGN", raise_if_not_found=False) or self.currency_id
        amount_ngn = self.currency_id._convert(
            self.amount, ngn, self.env.company, fields.Date.to_date(self.transaction_date)
        )
        threshold = config["high_value_threshold"]
        is_high_value = float_compare(amount_ngn, threshold, precision_rounding=ngn.rounding) >= 0
        if is_high_value:
            reasons.append(_(
                "High value: %(amount)s is at or above the review threshold of %(threshold)s.",
                amount=formatLang(self.env, amount_ngn, currency_obj=ngn),
                threshold=formatLang(self.env, threshold, currency_obj=ngn),
            ))

        is_high_frequency = recent_count > config["velocity_max_count"]
        if is_high_frequency:
            reasons.append(_(
                "High frequency: the customer made %(count)s transactions within %(minutes)s minutes (limit: %(limit)s).",
                count=recent_count,
                minutes=config["velocity_window_minutes"],
                limit=config["velocity_max_count"],
            ))

        if not reasons:
            return {}
        return {
            "is_high_value": is_high_value,
            "is_high_frequency": is_high_frequency,
            "flag_reason": "\n".join(reasons),
        }

    def _count_recent_transactions(self, window_minutes):
        if not self:
            return {}
        self.flush_model(["partner_id", "transaction_date"])
        self.env.cr.execute(
            """
            SELECT tx.id, COUNT(other.id)
              FROM bank_transaction tx
              JOIN bank_transaction other
                ON other.partner_id = tx.partner_id
               AND other.transaction_date >  tx.transaction_date - %s * INTERVAL '1 minute'
               AND other.transaction_date <= tx.transaction_date
             WHERE tx.id IN %s
          GROUP BY tx.id
            """,
            [window_minutes, tuple(self.ids)],
        )
        return dict(self.env.cr.fetchall())

    @api.model
    def _get_monitoring_config(self):
        get_param = self.env["ir.config_parameter"].sudo().get_param
        config = {}
        for key, default in DEFAULT_PARAMS.items():
            raw = get_param(PARAM_PREFIX + key)
            try:
                value = type(default)(raw) if raw not in (None, False, "") else default
                if value <= 0:
                    raise ValueError("must be greater than zero")
            except (TypeError, ValueError):
                _logger.warning("Invalid system parameter %s=%r, using default %r.", PARAM_PREFIX + key, raw, default)
                value = default
            config[key] = value
        return config
8
