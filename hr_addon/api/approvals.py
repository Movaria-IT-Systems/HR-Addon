# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Vacation, sick and home office requests waiting for the logged-in approver.

Leave uses the employee's Leave Approver (or the department's). Home office is
an Attendance Request with reason Work From Home and uses the same person.
HR Manager and System Manager can decide any request.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

from hr_addon.api.employee_app import _looks_like_illness
from hr_addon.hr_addon.leave_days import charged_leave_days

KINDS = {"leave": "Leave Application", "home_office": "Attendance Request"}


def classify(leave_type=None, reason=None):
	"""Calendar kind: sick, leave (vacation) or home."""
	if reason == "Work From Home":
		return "home"
	return "sick" if _looks_like_illness(leave_type) else "leave"


def _sees_all():
	return bool(set(frappe.get_roles()) & {"HR Manager", "System Manager"})


def _employees_for_approver():
	"""None means every employee. Otherwise the people this user approves."""
	if _sees_all():
		return None
	user = frappe.session.user
	names = set(
		frappe.get_all(
			"Employee",
			filters={"leave_approver": user, "status": "Active"},
			pluck="name",
			ignore_permissions=True,
		)
	)
	departments = frappe.get_all(
		"Department Approver",
		filters={"approver": user, "parentfield": "leave_approvers"},
		pluck="parent",
		ignore_permissions=True,
	)
	if departments:
		names.update(
			frappe.get_all(
				"Employee",
				filters={"department": ["in", departments], "status": "Active"},
				pluck="name",
				ignore_permissions=True,
			)
		)
	return names


def _worked_days(employee, start, end):
	rows = frappe.get_all(
		"Workday",
		filters={
			"employee": employee,
			"log_date": ["between", [getdate(start), getdate(end)]],
		},
		fields=["log_date", "actual_working_hours"],
		order_by="log_date",
		ignore_permissions=True,
	)
	return [
		{"date": str(getdate(row.log_date)), "hours": flt(row.actual_working_hours)}
		for row in rows
		if flt(row.actual_working_hours) > 0
	]


def _leave_payload(row):
	days = charged_leave_days(
		row.employee,
		row.leave_type,
		row.from_date,
		row.to_date,
		row.half_day,
		row.half_day_date,
	)
	return {
		"kind": classify(row.leave_type),
		"request_kind": "leave",
		"name": row.name,
		"employee": row.employee,
		"employee_name": row.employee_name,
		"department": row.department,
		"from_date": str(row.from_date),
		"to_date": str(row.to_date),
		"leave_type": row.leave_type,
		"days": flt(days),
		"calendar_days": date_diff(row.to_date, row.from_date) + 1,
		"half_day": bool(row.half_day),
		"note": row.description or "",
		"worked": _worked_days(row.employee, row.from_date, row.to_date),
	}


def _home_payload(row):
	return {
		"kind": "home",
		"request_kind": "home_office",
		"name": row.name,
		"employee": row.employee,
		"employee_name": row.employee_name,
		"department": row.department,
		"from_date": str(row.from_date),
		"to_date": str(row.to_date),
		"leave_type": None,
		"days": date_diff(row.to_date, row.from_date) + 1,
		"calendar_days": date_diff(row.to_date, row.from_date) + 1,
		"half_day": bool(row.half_day),
		"note": row.explanation or "",
		"worked": _worked_days(row.employee, row.from_date, row.to_date),
	}


@frappe.whitelist()
def get_pending_requests():
	"""Open vacation, sick and home office requests this user may decide."""
	if frappe.session.user in (None, "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	employees = _employees_for_approver()
	if employees is not None and not employees:
		return []

	leave_filters = {"docstatus": 0, "status": "Open"}
	home_filters = {"docstatus": 0, "reason": "Work From Home"}
	if employees is not None:
		leave_filters["employee"] = ["in", list(employees)]
		home_filters["employee"] = ["in", list(employees)]

	leaves = frappe.get_all(
		"Leave Application",
		filters=leave_filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"department",
			"leave_type",
			"from_date",
			"to_date",
			"half_day",
			"half_day_date",
			"description",
			"creation",
		],
		order_by="from_date asc",
		limit=100,
		ignore_permissions=True,
	)
	homes = frappe.get_all(
		"Attendance Request",
		filters=home_filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"department",
			"from_date",
			"to_date",
			"half_day",
			"explanation",
			"creation",
		],
		order_by="from_date asc",
		limit=100,
		ignore_permissions=True,
	)
	rows = [_leave_payload(row) for row in leaves] + [_home_payload(row) for row in homes]
	rows.sort(key=lambda row: (row["from_date"], row["employee_name"] or "", row["name"]))
	return rows


def _assert_can_decide(employee):
	if frappe.session.user in (None, "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	owner = frappe.db.get_value("Employee", employee, "user_id")
	if (
		owner == frappe.session.user
		and frappe.db.get_single_value("HR Settings", "prevent_self_leave_approval")
	):
		frappe.throw(_("You cannot approve your own request."))
	employees = _employees_for_approver()
	if employees is not None and employee not in employees:
		frappe.throw(_("You are not the approver for this employee."), frappe.PermissionError)


@frappe.whitelist()
def decide_request(kind, name, decision):
	"""Approve or reject one waiting request. `kind` is leave or home_office."""
	if decision not in ("approve", "reject"):
		frappe.throw(_("Unknown decision {0}").format(decision))
	if kind not in KINDS:
		frappe.throw(_("Unknown request type {0}").format(kind))

	doc = frappe.get_doc(KINDS[kind], name)
	if kind == "home_office" and doc.reason != "Work From Home":
		frappe.throw(_("This is not a home office request."))
	if doc.docstatus != 0 or (kind == "leave" and doc.status != "Open"):
		frappe.throw(_("This request was already decided."))

	_assert_can_decide(doc.employee)
	doc.flags.ignore_permissions = True
	if kind == "leave":
		doc.status = "Approved" if decision == "approve" else "Rejected"
		doc.submit()
	elif decision == "approve":
		doc.submit()
	else:
		frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True)
	return {"name": name, "decision": decision}
