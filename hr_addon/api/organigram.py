# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Company trees and HR roles for the employee app.

Organisation is the reporting line (Employee → Reports To). Approval is the
leave-approver line (Employee → Leave Approver). The roles on a card are the
HR roles of that person's user. Other ERPNext roles are left out.
"""

import frappe
from frappe import _

from hr_addon.api.employee_app import _current_employee

# Order is the order shown on the chart and in the legend.
LEGEND = (
	("Employee", "Own hours, calendar and requests in this app."),
	(
		"Employee Self Service",
		"Self-service role on the desk. This app finds the person through Employee → User ID.",
	),
	(
		"Leave Approver",
		"Can submit a Leave Application on the desk. Approvals in this app follow the Leave Approver field, not this role.",
	),
	("Expense Approver", "Approves expense claims."),
	(
		"HR User",
		"Reads and submits HR documents on the desk. Does not open every request on Approvals.",
	),
	(
		"HR Manager",
		"Sees and can decide every open vacation, sick and home office request on Approvals.",
	),
	(
		"System Manager",
		"Same full Approvals list as HR Manager, plus the rest of the desk.",
	),
)
SHOWN_ROLES = {role for role, _text in LEGEND}


def shown_roles(roles):
	"""HR roles in legend order."""
	have = set(roles or [])
	return [role for role, _text in LEGEND if role in have]


def acyclic_parents(people):
	"""Map employee name → reports_to, with cycles and unknown managers removed.

	A cycle A → B → A keeps one direction and drops the edge that closed the loop.
	"""
	parent = {person["name"]: person.get("reports_to") or None for person in people}
	names = set(parent)
	for name in names:
		seen = []
		current = name
		while current in names:
			if current in seen:
				parent[seen[-1]] = None
				break
			seen.append(current)
			nxt = parent.get(current)
			if nxt == current:
				parent[current] = None
				break
			if not nxt or nxt not in names:
				break
			current = nxt
	return parent


def approval_forest(people):
	"""Nest people under the employee named in Leave Approver.

	The parent is the colleague whose user is that Leave Approver. A user who is
	not an employee in this company, or the person themselves, leaves them at
	the top. Department approvers and HR Manager are not extra parents: one
	person would otherwise sit in several places.
	"""
	by_user = {}
	for person in people:
		user = person.get("user_id")
		if user:
			by_user.setdefault(user, person["name"])
	linked = []
	for person in people:
		parent = by_user.get(person.get("leave_approver"))
		if parent == person["name"]:
			parent = None
		linked.append({**person, "reports_to": parent})
	return build_forest(linked)


def build_forest(people):
	"""Nest people under Reports To. Every input person appears once."""
	parent = acyclic_parents(people)
	nodes = {
		person["name"]: {**person, "reports_to": parent.get(person["name"]), "children": []}
		for person in people
	}
	roots = []
	for node in nodes.values():
		manager = nodes.get(node["reports_to"])
		if manager and manager is not node:
			manager["children"].append(node)
		else:
			node["reports_to"] = None
			roots.append(node)
	key = lambda node: (node.get("employee_name") or "").lower()
	for node in nodes.values():
		node["children"].sort(key=key)
	roots.sort(key=key)
	return roots


def _legend():
	return [{"role": role, "text": text} for role, text in LEGEND]


@frappe.whitelist()
def get_my_hr_access():
	"""Roles and approval scope of the logged-in user, for the guide."""
	if frappe.session.user in (None, "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	user = frappe.session.user
	roles = frappe.get_roles(user)
	company = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "company")
	employee_filters = {"leave_approver": user, "status": "Active"}
	if company:
		employee_filters["company"] = company
	departments = frappe.get_all(
		"Department Approver",
		filters={"approver": user, "parentfield": "leave_approvers"},
		pluck="parent",
		ignore_permissions=True,
	)
	return {
		"roles": shown_roles(roles),
		"approves_employees": frappe.db.count("Employee", employee_filters),
		"approves_departments": sorted(set(departments)),
		"sees_all_approvals": bool(set(roles) & {"HR Manager", "System Manager"}),
	}


@frappe.whitelist()
def get_organigram():
	"""Active employees of the user's company, with reporting line and HR roles."""
	me = _current_employee()
	company = frappe.db.get_value("Employee", me, "company")
	employees = frappe.get_all(
		"Employee",
		filters={"company": company, "status": "Active"},
		fields=[
			"name",
			"employee_name",
			"designation",
			"department",
			"reports_to",
			"image",
			"user_id",
			"leave_approver",
		],
		order_by="employee_name asc",
		ignore_permissions=True,
	)
	people = _with_roles(employees, me)
	roots = build_forest(people)
	approvals = approval_forest(people)
	return {
		"company": company,
		"me": me,
		"has_reporting_line": any(node["children"] for node in roots),
		"has_approval_line": any(node["children"] for node in approvals),
		"legend": _legend(),
		"people": [_public_person(person) for person in people],
		"roots": _public_tree(roots),
		"approval_roots": _public_tree(approvals),
	}


def _public_person(person):
	hidden = {"children", "user_id", "leave_approver"}
	return {key: value for key, value in person.items() if key not in hidden}


def _public_tree(nodes):
	public = []
	for node in nodes:
		item = _public_person(node)
		item["children"] = _public_tree(node.get("children") or [])
		public.append(item)
	return public


def _with_roles(employees, me):
	user_ids = sorted({row.user_id for row in employees if row.user_id})
	approver_ids = sorted({row.leave_approver for row in employees if row.leave_approver})
	roles_by_user = _roles_by_user(user_ids)
	profiles = _profiles(user_ids)
	names = _user_names(sorted(set(approver_ids)))
	counts = {}
	for row in employees:
		if row.leave_approver:
			counts[row.leave_approver] = counts.get(row.leave_approver, 0) + 1
	departments = _department_approvers(user_ids)
	people = []
	for row in employees:
		user = row.user_id
		people.append(
			{
				"name": row.name,
				"employee_name": row.employee_name,
				"designation": row.designation,
				"department": row.department,
				"reports_to": row.reports_to,
				"image": row.image,
				"is_me": row.name == me,
				"roles": shown_roles(roles_by_user.get(user, [])),
				"role_profile": profiles.get(user),
				"user_id": user,
				"leave_approver": row.leave_approver,
				"leave_approver_name": names.get(row.leave_approver) if row.leave_approver else None,
				"approves_count": counts.get(user, 0) if user else 0,
				"department_approver_of": departments.get(user, []) if user else [],
			}
		)
	return people


def _roles_by_user(user_ids):
	grouped = {user: [] for user in user_ids}
	if not user_ids:
		return grouped
	for row in frappe.get_all(
		"Has Role",
		filters={"parent": ["in", user_ids], "parenttype": "User", "role": ["in", list(SHOWN_ROLES)]},
		fields=["parent", "role"],
		limit_page_length=0,
		ignore_permissions=True,
	):
		grouped.setdefault(row.parent, []).append(row.role)
	return grouped


def _profiles(user_ids):
	if not user_ids:
		return {}
	return {
		row.name: row.role_profile_name
		for row in frappe.get_all(
			"User",
			filters={"name": ["in", user_ids]},
			fields=["name", "role_profile_name"],
			limit_page_length=0,
			ignore_permissions=True,
		)
		if row.role_profile_name
	}


def _user_names(user_ids):
	if not user_ids:
		return {}
	return {
		row.name: row.full_name
		for row in frappe.get_all(
			"User",
			filters={"name": ["in", user_ids]},
			fields=["name", "full_name"],
			limit_page_length=0,
			ignore_permissions=True,
		)
	}


def _department_approvers(user_ids):
	grouped = {user: [] for user in user_ids}
	if not user_ids:
		return grouped
	rows = frappe.get_all(
		"Department Approver",
		filters={"approver": ["in", user_ids], "parentfield": "leave_approvers"},
		fields=["approver", "parent"],
		limit_page_length=0,
		ignore_permissions=True,
	)
	for row in rows:
		grouped.setdefault(row.approver, []).append(row.parent)
	for user, departments in grouped.items():
		grouped[user] = sorted(set(departments))
	return grouped
