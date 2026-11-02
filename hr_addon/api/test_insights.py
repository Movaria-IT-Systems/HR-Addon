# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from frappe.tests.utils import FrappeTestCase

from hr_addon.api.insights import (
	average_end,
	average_start,
	chart_points,
	hours_so_far,
	segments_for_day,
	summarize_project_bookings,
	span_bounds,
	_year_marks,
)


class TestInsightSpan(FrappeTestCase):
	def test_week_starts_on_monday(self):
		span, start, end = span_bounds("week", "2026-11-04")  # Wednesday
		self.assertEqual(span, "week")
		self.assertEqual(str(start), "2026-11-02")
		self.assertEqual(str(end), "2026-11-08")

	def test_unknown_span_is_the_month(self):
		span, start, end = span_bounds("quarter", "2026-11-04")
		self.assertEqual(span, "month")
		self.assertEqual(str(start), "2026-11-01")
		self.assertEqual(str(end), "2026-11-30")


class TestInsightHours(FrappeTestCase):
	def test_future_targets_stay_out_of_the_balance(self):
		days = [
			{"date": "2026-11-02", "target": 8, "actual": 7, "missing": False},
			{"date": "2026-11-03", "target": 8, "actual": 0, "missing": True},
			{"date": "2026-11-04", "target": 8, "actual": 0, "missing": False},
		]
		totals = hours_so_far(days, "2026-11-03")
		self.assertEqual(totals["target"], 16)
		self.assertEqual(totals["actual"], 7)
		self.assertEqual(totals["balance"], -9)
		self.assertEqual(totals["worked_days"], 1)
		self.assertEqual(totals["missing_days"], 1)

	def test_chart_keeps_each_day(self):
		points = chart_points(
			[{"date": "2026-01-15", "target": 8, "actual": 8}, {"date": "2026-01-16", "target": 8, "actual": 4}],
			"year",
			2026,
		)
		self.assertEqual(len(points), 2)
		self.assertEqual(points[0]["date"], "2026-01-15")
		self.assertEqual(points[0]["target"], 8)
		self.assertEqual(points[1]["actual"], 4)


class TestProjectBookings(FrappeTestCase):
	def test_groups_hours_by_project_and_activity(self):
		summary = summarize_project_bookings(
			[
				{"project": "PROJ-0003", "project_name": "Production", "activity_type": "Development", "hours": 8},
				{"project": "PROJ-0003", "project_name": "Production", "activity_type": "Operational", "hours": 2},
				{"project": "PROJ-0001", "project_name": "Internal", "activity_type": "Operational", "hours": 5},
			],
			worked=20,
		)
		self.assertEqual(summary["booked"], 15)
		self.assertEqual(summary["open"], 5)
		self.assertEqual(summary["over"], 0)
		self.assertEqual(summary["rows"][0]["name"], "Production")
		self.assertEqual(summary["rows"][0]["share"], 66.7)
		self.assertEqual(summary["rows"][0]["parts"][0]["activity"], "Development")
		self.assertEqual(summary["rows"][1]["hours"], 5)

	def test_marks_hours_booked_above_the_worked_time(self):
		summary = summarize_project_bookings(
			[{"project": "PROJ-0001", "project_name": "Internal", "activity_type": "Development", "hours": 6}],
			worked=4,
		)
		self.assertEqual(summary["open"], 0)
		self.assertEqual(summary["over"], 2)


class TestYearMarks(FrappeTestCase):
	def test_marks_a_sick_day_and_leaves_the_weekend_off(self):
		marks = _year_marks(
			[{"log_date": "2026-11-02", "status": "Present", "actual_working_hours": 8, "first_checkin": "2026-11-02 08:00:00"}],
			[{"leave_type": "Sick Leave", "from_date": "2026-11-03", "to_date": "2026-11-03", "docstatus": 1}],
			[],
			"2026-11-01",
			"2026-11-04",
			"2026-11-02",
		)
		kinds = {mark["date"]: mark["kind"] for mark in marks}
		self.assertEqual(kinds["2026-11-01"], "off")
		self.assertEqual(kinds["2026-11-02"], "work")
		self.assertEqual(kinds["2026-11-03"], "sick")
		self.assertEqual(kinds["2026-11-04"], "future")
		self.assertEqual(marks[1]["actual"], 8)


class TestCheckinSegments(FrappeTestCase):
	def test_pairs_in_and_out(self):
		segments = segments_for_day(
			[
				{"time": "2026-11-02 08:12:00", "log_type": "IN"},
				{"time": "2026-11-02 12:00:00", "log_type": "OUT"},
				{"time": "2026-11-02 12:31:00", "log_type": "IN"},
				{"time": "2026-11-02 16:40:00", "log_type": "OUT"},
			]
		)
		self.assertEqual(
			segments,
			[
				{"start": "08:12", "end": "12:00", "open": False},
				{"start": "12:31", "end": "16:40", "open": False},
			],
		)

	def test_open_punch_runs_until_now_today(self):
		segments = segments_for_day(
			[{"time": "2026-11-02 08:05:00", "log_type": "IN"}],
			now="2026-11-02 11:15:00",
			is_today=True,
		)
		self.assertEqual(segments, [{"start": "08:05", "end": "11:15", "open": True}])

	def test_average_start_uses_the_first_in(self):
		average = average_start(
			{
				"2026-11-02": [
					{"time": "2026-11-02 08:00:00", "log_type": "IN"},
					{"time": "2026-11-02 16:00:00", "log_type": "OUT"},
				],
				"2026-11-03": [{"time": "2026-11-03 09:00:00", "log_type": "IN"}],
				"2026-11-04": [{"time": "2026-11-04 07:00:00", "log_type": "IN"}],
			},
			"2026-11-03",
		)
		self.assertEqual(average, "08:30")

	def test_average_end_uses_the_last_out(self):
		average = average_end(
			{
				"2026-11-02": [
					{"time": "2026-11-02 08:00:00", "log_type": "IN"},
					{"time": "2026-11-02 12:00:00", "log_type": "OUT"},
					{"time": "2026-11-02 13:00:00", "log_type": "IN"},
					{"time": "2026-11-02 17:30:00", "log_type": "OUT"},
				],
				"2026-11-03": [
					{"time": "2026-11-03 08:15:00", "log_type": "IN"},
					{"time": "2026-11-03 16:30:00", "log_type": "OUT"},
				],
				"2026-11-04": [{"time": "2026-11-04 09:00:00", "log_type": "IN"}],
			},
			"2026-11-04",
		)
		self.assertEqual(average, "17:00")
