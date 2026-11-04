# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Personal insights for the employee signed in to the HR app.

Every figure is limited to that employee. Nothing here is a team report.
"""

import calendar
from datetime import datetime

import frappe
from frappe.utils import add_days, cint, date_diff, flt, get_datetime, getdate

from hr_addon.api.employee_app import (
	_current_employee,
	_days_for_range,
	_home_office_rows,
	_leave_rows,
	_looks_like_illness,
	_overtime_balance,
	_workday_rows,
)
from hr_addon.hr_addon.leave_days import _dates, _holiday_dates, charged_leave_days, is_zero_hour_day

SPANS = ("week", "month", "year", "days30", "custom")
# A custom range longer than this uses the year heatmap instead of one row per day.
DAILY_LIMIT = 62


def span_bounds(span, today, from_date=None, to_date=None):
	"""Monday–Sunday, the calendar month, the calendar year, the last 30 days, or a chosen range.

	The last 30 days end today. A custom range is swapped if the end is before the start,
	and cut to 366 days.
	"""
	today = getdate(today)
	if span == "custom":
		start, end = _custom_bounds(today, from_date, to_date)
		return "custom", start, end
	span = span if span in SPANS else "month"
	if span == "week":
		start = add_days(today, -today.weekday())
		return span, start, add_days(start, 6)
	if span == "year":
		return span, getdate(f"{today.year}-01-01"), getdate(f"{today.year}-12-31")
	if span == "days30":
		return span, add_days(today, -29), today
	last = calendar.monthrange(today.year, today.month)[1]
	return span, getdate(f"{today.year}-{today.month:02d}-01"), getdate(f"{today.year}-{today.month:02d}-{last}")


def uses_daily_timeline(span, start, end):
	"""One check-in row per day. Longer ranges use the compact year marks."""
	if span == "year":
		return False
	if span == "custom" and date_diff(end, start) + 1 > DAILY_LIMIT:
		return False
	return True


def _custom_bounds(today, from_date, to_date):
	try:
		start = getdate(from_date) if from_date else add_days(today, -29)
		end = getdate(to_date) if to_date else today
	except Exception:
		start, end = add_days(today, -29), today
	if end < start:
		start, end = end, start
	if date_diff(end, start) > 365:
		end = add_days(start, 365)
	return start, end


def segments_for_day(punches, now=None, is_today=False):
	"""Pair IN/OUT punches into {start, end, open} segments. Times are HH:MM."""
	segments = []
	open_start = None
	for punch in punches or []:
		kind = (punch.get("log_type") or "").upper()
		if kind not in ("IN", "OUT"):
			kind = "OUT" if open_start else "IN"
		stamp = get_datetime(punch.get("time"))
		if kind == "IN":
			if open_start:
				segments.append({"start": _hm(open_start), "end": _hm(open_start), "open": True})
			open_start = stamp
			continue
		if open_start:
			segments.append({"start": _hm(open_start), "end": _hm(stamp), "open": False})
			open_start = None
	if open_start:
		end = get_datetime(now) if is_today and now else open_start
		segments.append({"start": _hm(open_start), "end": _hm(end), "open": True})
	return segments


def chart_points(days, span=None, year=None):
	"""One point per day. `span` and `year` are unused and kept for callers."""
	return [
		{"date": day["date"], "target": flt(day.get("target"), 2), "actual": flt(day.get("actual"), 2)}
		for day in days
	]


def hours_so_far(days, today):
	"""Target and actual up to today. Later days stay on the chart, not in the balance."""
	today = getdate(today)
	target = actual = 0.0
	worked = missing = 0
	for day in days:
		if getdate(day["date"]) > today:
			continue
		target += flt(day.get("target"))
		actual += flt(day.get("actual"))
		if flt(day.get("actual")) > 0:
			worked += 1
		if day.get("missing"):
			missing += 1
	return {
		"target": flt(target, 2),
		"actual": flt(actual, 2),
		"balance": flt(actual - target, 2),
		"worked_days": worked,
		"missing_days": missing,
	}


def summarize_project_bookings(rows, worked):
	"""Group timesheet lines into projects, each split by activity."""
	by_project = {}
	for row in rows or []:
		project = row.get("project")
		if not project:
			continue
		bucket = by_project.setdefault(
			project,
			{
				"project": project,
				"name": row.get("project_name") or project,
				"hours": 0.0,
				"activities": {},
			},
		)
		hours = flt(row.get("hours"), 2)
		bucket["hours"] = flt(bucket["hours"] + hours, 2)
		activity = row.get("activity_type") or ""
		bucket["activities"][activity] = flt(bucket["activities"].get(activity, 0) + hours, 2)
	booked = flt(sum(bucket["hours"] for bucket in by_project.values()), 2)
	worked = flt(worked, 2)
	items = []
	for bucket in sorted(by_project.values(), key=lambda item: (-item["hours"], item["name"].lower())):
		parts = []
		for activity, hours in sorted(bucket["activities"].items(), key=lambda item: (-item[1], item[0])):
			parts.append(
				{
					"activity": activity,
					"hours": hours,
					"share": round(hours / bucket["hours"] * 100, 1) if bucket["hours"] else 0,
				}
			)
		items.append(
			{
				"project": bucket["project"],
				"name": bucket["name"],
				"hours": bucket["hours"],
				"share": round(bucket["hours"] / booked * 100, 1) if booked else 0,
				"parts": parts,
			}
		)
	return {
		"booked": booked,
		"worked": worked,
		"open": flt(max(worked - booked, 0), 2),
		"over": flt(max(booked - worked, 0), 2),
		"rows": items,
	}


def _project_booking_rows(employee, start, end):
	return frappe.db.sql(
		"""
		SELECT
			td.project,
			COALESCE(NULLIF(td.project_name, ''), td.project) AS project_name,
			td.activity_type,
			SUM(td.hours) AS hours
		FROM `tabTimesheet Detail` td
		INNER JOIN `tabTimesheet` t ON t.name = td.parent
		WHERE t.employee = %(employee)s
			AND t.docstatus = 1
			AND td.from_time >= %(start)s
			AND td.from_time < %(end)s
			AND IFNULL(td.project, '') != ''
		GROUP BY td.project, project_name, td.activity_type
		""",
		{
			"employee": employee,
			"start": f"{getdate(start)} 00:00:00",
			"end": f"{add_days(getdate(end), 1)} 00:00:00",
		},
		as_dict=True,
	)


def average_start(punches_by_day, today):
	"""Mean clock time of the first IN punch, as HH:MM. Empty when nobody checked in."""
	return _average_clock(punches_by_day, today, "IN")


def average_end(punches_by_day, today):
	"""Mean clock time of the last OUT punch, as HH:MM. Days still open are skipped."""
	return _average_clock(punches_by_day, today, "OUT")


def _average_clock(punches_by_day, today, kind):
	today = getdate(today)
	want = kind.upper()
	minutes = []
	for day, punches in (punches_by_day or {}).items():
		if getdate(day) > today:
			continue
		chosen = None
		for punch in punches or []:
			log_type = (punch.get("log_type") or "IN").upper()
			if log_type != want:
				continue
			stamp = get_datetime(punch.get("time"))
			chosen = stamp.hour * 60 + stamp.minute
			if want == "IN":
				break
		if chosen is not None:
			minutes.append(chosen)
	if not minutes:
		return ""
	average = int(round(sum(minutes) / len(minutes)))
	return f"{average // 60:02d}:{average % 60:02d}"


def _hm(value):
	stamp = get_datetime(value)
	return stamp.strftime("%H:%M")


def _day_kind(day):
	leave = day.get("leave") or {}
	if leave:
		return "sick" if leave.get("illness") else "leave"
	if day.get("home_office"):
		return "home"
	status = day.get("status") or ""
	if status in ("Missing", "Missing Checkin", "Absent"):
		return "missing"
	if status == "Holiday":
		return "holiday"
	if status in ("Off", "Not Workday") or day.get("schedule") == "free":
		return "off"
	if flt(day.get("actual_working_hours")) or day.get("first_checkin"):
		return "work"
	return "open"


def _year_marks(workdays, leaves, homes, start, end, today):
	"""One small mark per calendar day, for the year heatmap.

	Built from the workdays and requests already loaded. It does not walk the
	weekly schedule, so a year view stays one query deep.
	"""
	today = getdate(today)
	start, end = getdate(start), getdate(end)
	rows = {_stamp(row, "log_date"): row for row in workdays}
	leave_on = _requests_by_day(leaves, start, end)
	home_on = _requests_by_day(homes, start, end)
	marks = []
	current = start
	while current <= end:
		key = str(current)
		row = rows.get(key)
		leave = leave_on.get(key)
		home = home_on.get(key)
		day = {
			"date": key,
			"status": _field(row, "status", "") or "",
			"actual_working_hours": flt(_field(row, "actual_working_hours", 0)),
			"first_checkin": _field(row, "first_checkin"),
			"schedule": "free" if row is None and current.weekday() >= 5 else "",
			"leave": (
				{"illness": _looks_like_illness(_field(leave, "leave_type", "")), "docstatus": cint(_field(leave, "docstatus", 1))}
				if leave
				else None
			),
			"home_office": {"docstatus": cint(_field(home, "docstatus", 1))} if home else None,
		}
		kind = _day_kind(day)
		if row is None and leave is None and home is None and current > today:
			kind = "future"
		pending = bool(
			(leave and cint(_field(leave, "docstatus", 1)) == 0 and kind in ("sick", "leave"))
			or (home and cint(_field(home, "docstatus", 1)) == 0 and kind == "home")
		)
		marks.append(
			{
				"date": key,
				"kind": kind,
				"pending": pending,
				"actual": flt(day["actual_working_hours"], 2),
			}
		)
		current = add_days(current, 1)
	return marks


def _requests_by_day(rows, start, end):
	"""Map each day of a request onto that request. Weekends stay unmarked."""
	covered = {}
	for row in rows or []:
		first = max(getdate(_field(row, "from_date")), start)
		last = min(getdate(_field(row, "to_date")), end)
		if last < first:
			continue
		for day in _dates(first, last):
			if day.weekday() >= 5:
				continue
			covered[str(day)] = row
	return covered


def _field(row, key, default=None):
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _stamp(row, key):
	value = _field(row, key)
	return str(getdate(value)) if value else ""


def _timeline_day(day, punches, today, now):
	current = getdate(day["date"])
	kind = _day_kind(day)
	segments = segments_for_day(punches, now=now, is_today=current == getdate(today))
	if not segments and day.get("first_checkin"):
		end = day.get("last_checkout") or day.get("first_checkin")
		segments = [{"start": _hm(day["first_checkin"]), "end": _hm(end), "open": not day.get("last_checkout")}]
	return {
		"date": day["date"],
		"kind": kind,
		"pending": bool((day.get("leave") or {}).get("docstatus") == 0 or (day.get("home_office") or {}).get("docstatus") == 0),
		"target": flt(day.get("target_hours"), 2),
		"actual": flt(day.get("actual_working_hours"), 2),
		"segments": segments,
	}


def _chart_day(day):
	return {
		"date": day["date"],
		"target": flt(day.get("target_hours"), 2),
		"actual": flt(day.get("actual_working_hours"), 2),
		"missing": bool(day.get("needs_attention")),
	}


def _leave_totals(employee, rows, start, end):
	taken = {"sick": 0.0, "leave": 0.0}
	pending = {"sick": 0.0, "leave": 0.0}
	for row in rows:
		overlap_start = max(getdate(row.from_date), start)
		overlap_end = min(getdate(row.to_date), end)
		if overlap_end < overlap_start:
			continue
		half_date = row.half_day_date if row.half_day and row.half_day_date else None
		if half_date and not (overlap_start <= getdate(half_date) <= overlap_end):
			half_date = None
		days = charged_leave_days(
			employee,
			row.leave_type,
			overlap_start,
			overlap_end,
			1 if half_date else 0,
			half_date,
		)
		kind = "sick" if _looks_like_illness(row.leave_type) else "leave"
		bucket = taken if cint(row.docstatus) == 1 or row.status == "Approved" else pending
		bucket[kind] += days
	return (
		flt(taken["sick"], 2),
		flt(pending["sick"], 2),
		flt(taken["leave"], 2),
		flt(pending["leave"], 2),
	)


def _home_office_totals(employee, rows, start, end):
	if not rows:
		return 0.0, 0.0
	holidays = _holiday_dates(employee, start, end)
	approved = {}
	waiting = {}
	for row in rows:
		overlap_start = max(getdate(row.from_date), start)
		overlap_end = min(getdate(row.to_date), end)
		if overlap_end < overlap_start:
			continue
		half = getdate(row.half_day_date) if row.half_day and row.half_day_date else None
		bucket = approved if cint(row.docstatus) == 1 else waiting
		for day in _dates(overlap_start, overlap_end):
			if half and day != half:
				continue
			if str(day) in holidays or is_zero_hour_day(employee, day):
				continue
			bucket[day] = 0.5 if half else 1.0
	return flt(sum(approved.values()), 2), flt(sum(waiting.values()), 2)


def _balances(employee, today):
	from hrms.hr.doctype.leave_application.leave_application import get_leave_details

	allocation = (get_leave_details(employee, today) or {}).get("leave_allocation") or {}
	rows = []
	for leave_type, info in allocation.items():
		rows.append(
			{
				"leave_type": leave_type,
				"kind": "sick" if _looks_like_illness(leave_type) else "leave",
				"remaining": flt(info.get("remaining_leaves"), 2),
				"allocated": flt(info.get("total_leaves"), 2),
				"pending": flt(info.get("leaves_pending_approval"), 2),
			}
		)
	return rows


def _sum_balances(rows, kind):
	chosen = [row for row in rows if row["kind"] == kind]
	if not chosen:
		return {"remaining": 0, "allocated": 0, "pending": 0, "has_allocation": False}
	return {
		"remaining": flt(sum(row["remaining"] for row in chosen), 2),
		"allocated": flt(sum(row["allocated"] for row in chosen), 2),
		"pending": flt(sum(row["pending"] for row in chosen), 2),
		"has_allocation": True,
	}


def _upcoming(leaves, homes, today):
	today = getdate(today)
	candidates = []
	for row in leaves:
		if row.status in ("Rejected", "Cancelled") or getdate(row.to_date) < today:
			continue
		candidates.append((getdate(row.from_date), "sick" if _looks_like_illness(row.leave_type) else "leave", row))
	for row in homes:
		if getdate(row.to_date) < today:
			continue
		candidates.append((getdate(row.from_date), "home", row))
	if not candidates:
		return None
	candidates.sort(key=lambda item: (0 if item[0] <= today else 1, item[0]))
	start, kind, row = candidates[0]
	return {
		"kind": kind,
		"from_date": str(getdate(row.from_date)),
		"to_date": str(getdate(row.to_date)),
		"pending": cint(getattr(row, "docstatus", 0)) == 0 and getattr(row, "status", "") != "Approved",
		"started": start <= today,
	}


def _punches(employee, start, end):
	rows = frappe.get_all(
		"Employee Checkin",
		filters={"employee": employee, "time": ["between", [f"{start} 00:00:00", f"{end} 23:59:59"]]},
		fields=["time", "log_type"],
		order_by="time asc",
		limit_page_length=4000,
		ignore_permissions=True,
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(str(getdate(row.time)), []).append(row)
	return grouped


@frappe.whitelist()
def get_my_insights(span="month", from_date=None, to_date=None):
	employee = _current_employee()
	today = getdate()
	now = datetime.now()
	span, start, end = span_bounds(span, today, from_date, to_date)
	leaves = _leave_rows(employee, start, end)
	homes = _home_office_rows(employee, start, end)
	sick, sick_pending, vacation_taken, vacation_pending = _leave_totals(employee, leaves, start, end)
	home_office, home_pending = _home_office_totals(employee, homes, start, end)
	balances = _balances(employee, today)

	punches = _punches(employee, start, end)
	dense = uses_daily_timeline(span, start, end)
	if not dense:
		rows = _workday_rows(employee, start, end)
		chart_days = [
			{
				"date": str(getdate(row.log_date)),
				"target": flt(row.target_hours, 2),
				"actual": flt(row.actual_working_hours, 2),
				"missing": row.status in ("Missing", "Missing Checkin", "Absent") and getdate(row.log_date) <= today,
			}
			for row in rows
		]
		# One mark per day for the year heatmap. Week and month keep the clock rows.
		timeline = _year_marks(rows, leaves, homes, start, end, today)
	else:
		days = _days_for_range(employee, start, end)
		chart_days = [_chart_day(day) for day in days]
		timeline = [_timeline_day(day, punches.get(day["date"], []), today, now) for day in days]

	totals = hours_so_far(chart_days, today)
	overtime_enabled = bool(cint(frappe.db.get_single_value("HR Addon Settings", "enable_overtime_ledger_feature")))
	return {
		"span": span,
		"dense": dense,
		"from_date": str(start),
		"to_date": str(end),
		"today": str(today),
		"sick": sick,
		"sick_pending": sick_pending,
		"vacation_taken": vacation_taken,
		"vacation_pending": vacation_pending,
		"home_office": home_office,
		"home_office_pending": home_pending,
		"vacation": _sum_balances(balances, "leave"),
		"sick_balance": _sum_balances(balances, "sick"),
		"balances": balances,
		"target": totals["target"],
		"actual": totals["actual"],
		"balance": totals["balance"],
		"worked_days": totals["worked_days"],
		"missing_days": totals["missing_days"],
		"average_start": average_start(punches, today),
		"average_end": average_end(punches, today),
		"projects": summarize_project_bookings(_project_booking_rows(employee, start, end), totals["actual"]),
		"overtime_enabled": overtime_enabled,
		"overtime_balance": _overtime_balance(employee) if overtime_enabled else None,
		"chart": chart_points(chart_days, span, today.year if span == "year" else None),
		"timeline": timeline,
		"timeline_span": span,
		"upcoming": _upcoming(
			_leave_rows(employee, today, add_days(today, 120)),
			_home_office_rows(employee, today, add_days(today, 120)),
			today,
		),
	}
