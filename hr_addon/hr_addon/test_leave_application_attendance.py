# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from hr_addon.overrides.custom_leave_application import HrAddonLeaveApplication
from hrms.hr.doctype.leave_application.leave_application import AttendanceAlreadyMarkedError


def _application():
	# Skip Document.__init__. Loading Leave Application meta fails in this site's test runner.
	doc = HrAddonLeaveApplication.__new__(HrAddonLeaveApplication)
	doc.employee = "HR-EMP-00001"
	doc.leave_type = "Privilege Leave"
	doc.from_date = "2026-09-28"
	doc.to_date = "2026-09-29"
	doc.status = "Open"
	return doc


class TestLeaveAttendanceConflict(FrappeTestCase):
	@patch.object(HrAddonLeaveApplication, "_date_charges_leave", return_value=True)
	@patch("hr_addon.overrides.custom_leave_application.frappe.get_all")
	def test_present_attendance_on_a_working_day_does_not_block_leave(self, mock_get_all, _charges):
		mock_get_all.return_value = [
			SimpleNamespace(name="ATT-1", attendance_date="2026-09-28", status="Present"),
			SimpleNamespace(name="ATT-2", attendance_date="2026-09-29", status="Present"),
		]
		_application().validate_attendance()

	@patch.object(HrAddonLeaveApplication, "_date_charges_leave", return_value=False)
	@patch("hr_addon.overrides.custom_leave_application.frappe.get_all")
	def test_attendance_on_a_free_day_does_not_block_leave(self, mock_get_all, _charges):
		mock_get_all.return_value = [
			SimpleNamespace(name="ATT-1", attendance_date="2026-09-26", status="Present"),
			SimpleNamespace(name="ATT-2", attendance_date="2026-09-27", status="Work From Home"),
		]
		_application().validate_attendance()

	@patch.object(HrAddonLeaveApplication, "_date_charges_leave", return_value=True)
	@patch("hr_addon.overrides.custom_leave_application.frappe.get_all")
	def test_work_from_home_on_a_working_day_still_blocks_leave(self, mock_get_all, _charges):
		mock_get_all.return_value = [
			SimpleNamespace(name="ATT-1", attendance_date="2026-09-28", status="Work From Home"),
		]
		self.assertRaises(AttendanceAlreadyMarkedError, _application().validate_attendance)


class TestLeaveApprovalKeepsWorkedFreeDay(FrappeTestCase):
	@patch.object(HrAddonLeaveApplication, "create_or_update_attendance")
	@patch("hr_addon.overrides.custom_leave_application.frappe.delete_doc")
	@patch("hr_addon.overrides.custom_leave_application.frappe.get_doc")
	@patch("hr_addon.overrides.custom_leave_application.frappe.db.exists", return_value="ATT-1")
	@patch("hr_addon.overrides.custom_leave_application.is_zero_hour_day", return_value=True)
	@patch("hr_addon.overrides.custom_leave_application.get_holiday_dates_for_employee", return_value=[])
	@patch("hr_addon.overrides.custom_leave_application.frappe.db.get_value", return_value=0)
	def test_approving_leave_keeps_present_attendance_on_a_free_day(
		self, _include, _holidays, _zero, _exists, mock_get_doc, mock_delete, mock_mark
	):
		attendance = SimpleNamespace(status="Present", docstatus=1, cancel=lambda: None)
		mock_get_doc.return_value = attendance
		doc = _application()
		doc.status = "Approved"
		doc.from_date = "2026-09-26"
		doc.to_date = "2026-09-26"
		doc.update_attendance()
		mock_get_doc.assert_called_once()
		mock_delete.assert_not_called()
		mock_mark.assert_not_called()
