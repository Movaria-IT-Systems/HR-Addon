# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from frappe.tests.utils import FrappeTestCase

from hr_addon.api.approvals import classify


class TestApprovalKind(FrappeTestCase):
	def test_sick_vacation_and_home_office(self):
		self.assertEqual(classify("Sick Leave"), "sick")
		self.assertEqual(classify("Krankmeldung"), "sick")
		self.assertEqual(classify("Privilege Leave"), "leave")
		self.assertEqual(classify(reason="Work From Home"), "home")
