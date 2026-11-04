# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from frappe.tests.utils import FrappeTestCase

from hr_addon.api.organigram import approval_forest, build_forest, shown_roles


def _person(name, reports_to=None):
	return {"name": name, "employee_name": name, "reports_to": reports_to}


def _names(nodes):
	found = []

	def walk(node):
		found.append(node["name"])
		for child in node["children"]:
			walk(child)

	for node in nodes:
		walk(node)
	return found


class TestOrganigram(FrappeTestCase):
	def test_roles_follow_legend_order(self):
		self.assertEqual(
			shown_roles(["System Manager", "Employee", "Accounts User", "HR Manager"]),
			["Employee", "HR Manager", "System Manager"],
		)

	def test_reporting_line(self):
		roots = build_forest(
			[
				_person("ada"),
				_person("bea", "ada"),
				_person("cy", "bea"),
			]
		)
		self.assertEqual([node["name"] for node in roots], ["ada"])
		self.assertEqual(roots[0]["children"][0]["name"], "bea")
		self.assertEqual(roots[0]["children"][0]["children"][0]["name"], "cy")

	def test_unknown_manager_is_a_root(self):
		roots = build_forest([_person("ada", "missing")])
		self.assertEqual(roots[0]["reports_to"], None)
		self.assertEqual(_names(roots), ["ada"])

	def test_approval_line_uses_leave_approver(self):
		people = [
			{"name": "max", "employee_name": "Max", "user_id": "max@example.com", "leave_approver": "admin@example.com"},
			{"name": "lea", "employee_name": "Lea", "user_id": "lea@example.com", "leave_approver": "max@example.com"},
			{"name": "pia", "employee_name": "Pia", "user_id": "pia@example.com", "leave_approver": "outsider@example.com"},
			{"name": "admin", "employee_name": "Admin", "user_id": "admin@example.com", "leave_approver": "admin@example.com"},
		]
		roots = approval_forest(people)
		by_name = {node["name"]: node for node in roots}
		self.assertIn("admin", by_name)
		self.assertEqual([child["name"] for child in by_name["admin"]["children"]], ["max"])
		self.assertEqual(by_name["admin"]["children"][0]["children"][0]["name"], "lea")
		self.assertIn("pia", by_name)
		self.assertEqual(by_name["pia"]["children"], [])
		self.assertEqual(sorted(_names(roots)), ["admin", "lea", "max", "pia"])

	def test_cycle_is_opened(self):
		roots = build_forest([_person("ada", "bea"), _person("bea", "ada")])
		self.assertEqual(sorted(_names(roots)), ["ada", "bea"])
		self.assertEqual(len(_names(roots)), 2)
