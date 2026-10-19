import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class ChinaCashFlowAssignment(Document):
	def load_doc_before_save(self, *, raise_exception=False):
		from china_finance.services.cash_flow_assignment import lock_assignment_sources

		# Frappe calls this before its native assignment FOR UPDATE, for inserts,
		# saves, submits and cancels. before_validate/before_submit are too late.
		lock_assignment_sources([self])
		return super().load_doc_before_save(raise_exception=raise_exception)

	IMMUTABLE_SOURCE_FIELDS = (
		"company",
		"posting_date",
		"china_accounting_voucher",
		"source_doctype",
		"source_name",
		"revision",
		"assignment_key",
	)
	ALLOCATION_FIELDS = (
		"gl_entry",
		"cash_account",
		"cash_direction",
		"cash_amount",
		"counterpart_accounts",
		"cash_flow_row_code",
		"assigned_amount",
		"remarks",
	)
	LOCKED_ITEM_FIELDS = (
		"name",
		"idx",
		*ALLOCATION_FIELDS,
		"suggested_row_code",
		"suggested_row_label",
		"cash_flow_row_label",
	)

	def _item_snapshot(self, fields):
		def value(row, field):
			if field in ("cash_amount", "assigned_amount"):
				return flt(row.get(field), 2)
			if field == "idx":
				return int(row.get(field) or 0)
			return row.get(field) or ""

		# Frappe loads old and new child rows as different Document instances.
		# Compare persisted values instead of child-object identity. Allocation
		# order and row IDs do not change who last specified the business values.
		return sorted(tuple(value(row, field) for field in fields) for row in self.items)

	def _allocation_has_changed(self, old):
		previous = old
		if {row.gl_entry for row in self.items} != {row.gl_entry for row in old.items}:
			from china_finance.services.cash_flow_assignment import (
				_get_voucher,
				_refresh_draft_cash_legs,
				get_cash_legs_for_voucher,
			)

			# Bind every allocation to its GL leg. Only normalize a changed GL
			# identity through the established repair, on a copy: the native old
			# document must remain intact for version and audit generation.
			previous = frappe.get_doc(old.as_dict())
			_refresh_draft_cash_legs(
				previous, get_cash_legs_for_voucher(_get_voucher(self.china_accounting_voucher, for_update=True), for_update=True)
			)
		return self._item_snapshot(self.ALLOCATION_FIELDS) != previous._item_snapshot(self.ALLOCATION_FIELDS)

	def before_insert(self):
		from china_finance.services.cash_flow_assignment import _get_active_assignment, _next_revision

		if self.status != "Draft" or self.docstatus != 0:
			frappe.throw(_("新建现金流量指定单必须为草稿，确认必须通过原生提交"))
		# Native imports/API inserts share the early source gate too. A unique
		# revision key alone does not prevent two effective assignments per voucher.
		if _get_active_assignment(self.china_accounting_voucher, for_update=True):
			frappe.throw(_("该凭证已有有效的现金流量指定单"))
		if not self.revision:
			self.revision = _next_revision(self.china_accounting_voucher)
		if not self.assignment_key:
			self.assignment_key = f"{self.china_accounting_voucher}|{int(self.revision)}"

	def validate(self):
		old = self.get_doc_before_save()
		if old:
			for fieldname in self.IMMUTABLE_SOURCE_FIELDS:
				if self.has_value_changed(fieldname):
					frappe.throw(_("现金流量指定单的来源信息不能修改"))
		if old and old.status != self.status and not self.flags.ignore_cash_flow_assignment_status:
			frappe.throw(_("现金流量指定单状态只能通过指定服务变更"))
		# A draft becomes Confirmed in the same save operation. Only records that
		# were already locked before this save must reject item changes.
		if (
			old
			and old.status != "Draft"
			and self._item_snapshot(self.LOCKED_ITEM_FIELDS) != old._item_snapshot(self.LOCKED_ITEM_FIELDS)
		):
			frappe.throw(_("已确认或已作废的现金流量指定单不能修改明细"))
		if self.status == "Draft":
			from china_finance.services.cash_flow_assignment import get_assignment_cash_legs, validate_cash_evidence

			validate_cash_evidence(self, get_assignment_cash_legs(self), old)
			if not old or self._allocation_has_changed(old) or self.has_value_changed("remarks"):
				self.assigned_by = frappe.session.user
				self.assigned_on = now_datetime()
			else:
				# Refreshing labels, suggestions or a repaired GL link is a system
				# update, not a new manual allocation. Preserve the trusted audit
				# fields as well as the preparer when another user only confirms.
				self.assigned_by = old.assigned_by
				self.assigned_on = old.assigned_on
		for row in self.items:
			row.cash_amount = flt(row.cash_amount, 2)
			row.assigned_amount = flt(row.assigned_amount, 2)

	def before_submit(self):
		from china_finance.services.cash_flow_assignment import prepare_cash_flow_assignment_confirmation

		prepare_cash_flow_assignment_confirmation(self)

	def before_update_after_submit(self):
		# Frappe skips validate() for submitted-document updates. These fields
		# remain allow_on_submit so native submit/cancel can write their audit,
		# but an ordinary submitted save must never change the effective state.
		for fieldname in (
			"status", "confirmed_by", "confirmed_on", "cancelled_by", "cancelled_on", "cancellation_reason"
		):
			if self.has_value_changed(fieldname):
				frappe.throw(_("已提交现金流量指定单的状态或审计信息不能修改"))

	def before_cancel(self):
		self.status = "Cancelled"
		self.cancelled_by = frappe.session.user
		self.cancelled_on = now_datetime()
		self.cancellation_reason = self.cancellation_reason or _("手工或来源单据作废")
		self.flags.ignore_cash_flow_assignment_status = True
