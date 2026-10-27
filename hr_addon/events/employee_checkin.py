import frappe
from functools import partial

from frappe.utils import getdate

SYNC_FLAG = "in_workday_checkin_sync"
# A workday rebuild writes attendance and the overtime ledger, so it stays
# running for a while. A second punch in that window must not be discarded.
_SYNC_ATTEMPTS = 5


def is_checkin_sync_enabled():
	return bool(
		frappe.db.get_single_value("HR Addon Settings", "update_workday_on_employee_checkin")
	)


def enqueue_workday_sync(doc, method=None):
	"""Refresh the Workday of a checkin's date whenever that checkin changes.

	Attendance created from a Workday writes itself back into the Employee Checkin, so a
	sync triggered from within our own save would recurse. SYNC_FLAG breaks that cycle.
	"""
	if getattr(frappe.flags, SYNC_FLAG, False):
		return

	if not doc.employee or not doc.time:
		return

	if not is_checkin_sync_enabled():
		return

	dates = {getdate(doc.time)}

	# A corrected timestamp can move the checkin to another day; both days need a refresh.
	previous = doc.get_doc_before_save() if method != "on_trash" else None
	if previous and previous.time:
		dates.add(getdate(previous.time))

	for log_date in dates:
		# Queue only after this punch is committed, and record that a newer
		# punch exists so a rebuild that is already running does one more pass.
		frappe.db.after_commit.add(
			partial(_queue_workday_sync, doc.employee, str(log_date))
		)


def _queue_workday_sync(employee, log_date):
	log_date = str(getdate(log_date))
	_bump_sync_generation(employee, log_date)
	frappe.enqueue(
		"hr_addon.events.employee_checkin.sync_workday",
		queue="short",
		enqueue_after_commit=False,
		job_id=f"workday-checkin-sync::{employee}::{log_date}",
		deduplicate=True,
		employee=employee,
		log_date=log_date,
	)


def _enqueue_sync_followup(employee, log_date):
	"""One more rebuild once the worker that holds this day is finished.

	Deduped on its own id, so a running rebuild does not swallow it.
	"""
	log_date = str(getdate(log_date))
	frappe.enqueue(
		"hr_addon.events.employee_checkin.sync_workday",
		queue="short",
		enqueue_after_commit=False,
		job_id=f"workday-checkin-sync::{employee}::{log_date}::followup",
		deduplicate=True,
		employee=employee,
		log_date=log_date,
	)


def sync_workday(employee, log_date):
	"""Recalculate one Workday from its Employee Checkins, creating it when missing.

	Punches that land while a rebuild is in progress bump a counter. This runs
	again until that counter stays still, so the saved day matches the punches.
	"""
	log_date = getdate(log_date)
	locked = _acquire_sync_lock(employee, log_date)
	if locked is False:
		_enqueue_sync_followup(employee, log_date)
		return

	try:
		for _attempt in range(_SYNC_ATTEMPTS):
			seen = _sync_generation(employee, log_date)
			_sync_workday_once(employee, log_date)
			if _sync_generation(employee, log_date) == seen:
				return
		_enqueue_sync_followup(employee, log_date)
	finally:
		if locked:
			_release_sync_lock(employee, log_date)


def _sync_workday_once(employee, log_date):
	from hr_addon.hr_addon.doctype.workday.workday import has_weekly_working_hours_for_date

	log_date = getdate(log_date)

	if not frappe.db.exists("Employee", {"name": employee, "status": "Active"}):
		return

	if not has_weekly_working_hours_for_date(employee, log_date):
		return

	workday_name = frappe.db.get_value(
		"Workday", {"employee": employee, "log_date": log_date}, "name"
	)

	if not workday_name and not has_checkins(employee, log_date):
		return

	setattr(frappe.flags, SYNC_FLAG, True)
	# Desk alerts ("Attendance updated", "Overtime Ledger Entry created") are for
	# the HR form. During an employee check-in they would be returned with the
	# request and the app would show them as errors.
	previous_mute = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	# A failed workday update must not undo the check-in the employee just saved.
	# Attendance and the overtime ledger commit inside the save, which drops this
	# savepoint. Releasing or rolling it back afterwards must not become the error.
	save_point = "workday_checkin_sync"
	frappe.db.savepoint(save_point)
	try:
		if workday_name:
			workday = frappe.get_doc("Workday", workday_name)
		else:
			workday = frappe.new_doc("Workday")
			workday.employee = employee
			workday.log_date = log_date
			workday.company = frappe.db.get_value("Employee", employee, "company")

		workday.flags.ignore_permissions = True
		workday.save()
	except Exception:
		_end_sync_savepoint(save_point, rollback=True)
		frappe.log_error(
			title="HR Addon: Workday sync from Employee Checkin",
			message=f"Employee {employee}, date {log_date}\n\n{frappe.get_traceback()}",
		)
	else:
		_end_sync_savepoint(save_point, rollback=False)
		frappe.db.commit()
	finally:
		frappe.flags.mute_messages = previous_mute
		setattr(frappe.flags, SYNC_FLAG, False)
		# Drop desk notes such as "No hour variance to record" so the app does not
		# show them as an error and skip refreshing the day.
		frappe.clear_messages()


def _end_sync_savepoint(save_point, rollback):
	"""Release or roll back the sync savepoint.

	A commit inside the workday save already removed it. That is not a failure:
	the check-in and the ledger row are stored.
	"""
	try:
		if rollback:
			frappe.db.rollback(save_point=save_point)
		else:
			frappe.db.release_savepoint(save_point)
	except Exception as exc:
		if "SAVEPOINT" not in str(exc) or "does not exist" not in str(exc):
			raise


def has_checkins(employee, log_date):
	# Same date match the workday uses. A raw between-filter can miss a punch
	# the calendar still lists, and the day is then left open.
	from hr_addon.hr_addon.doctype.workday.workday import get_employee_checkin

	return bool(get_employee_checkin(employee, log_date))


def _sync_redis_key(employee, log_date, suffix):
	return frappe.cache().make_key(
		f"hr_addon:workday-sync:{suffix}:{employee}:{getdate(log_date)}"
	)


def _bump_sync_generation(employee, log_date):
	try:
		cache = frappe.cache()
		key = _sync_redis_key(employee, log_date, "gen")
		value = cache.incr(key)
		cache.expire(key, 6 * 60 * 60)
		return int(value)
	except Exception:
		return None


def _sync_generation(employee, log_date):
	try:
		value = frappe.cache().get(_sync_redis_key(employee, log_date, "gen"))
		return int(value) if value is not None else 0
	except Exception:
		return 0


def _acquire_sync_lock(employee, log_date):
	"""True when this worker owns the day. False when another rebuild does. None without Redis."""
	try:
		key = _sync_redis_key(employee, log_date, "lock")
		return bool(frappe.cache().set(key, b"1", nx=True, ex=180))
	except Exception:
		return None


def _release_sync_lock(employee, log_date):
	try:
		frappe.cache().delete(_sync_redis_key(employee, log_date, "lock"))
	except Exception:
		return
