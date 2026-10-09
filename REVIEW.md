# Starter review

References below identify functions in the original starter `app/main.py` supplied with the assignment kit. The submission implementation is `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How it is fixed |
|---|---|---|---|---|
| 1 | MongoClient setup; timestamp helpers | Default naive BSON datetimes and host-local datetimes leave the time zone implicit. | Run on a machine outside IST; punches are assigned to incorrect dates and shift calculations differ. | Aware UTC MongoClient, aware UTC clock and epoch conversion, explicit IST calendar calculations. |
| 2 | Startup / index setup | No indexes, including no uniqueness guarantees. | Parallel creates duplicate codes/days; 100k-row queries scan collections. | Idempotent unique natural-key and query-supporting indexes at startup. |
| 3 | `health` | Always reports ready without contacting MongoDB. | Stop MongoDB: starter still returns 200. | Ping MongoDB; return 503 on database failure. |
| 4 | `EmployeeIn` | No employee-code, length, email, calendar-date or shift-time validation. | `EMPx`, blank name, `a@b`, `2026-02-30` or `25:00` is accepted. | Constrained fields and strict calendar validation return 422. |
| 5 | `EmployeeIn` | Equal shift start/end is accepted although employee creation forbids it. | Create a 09:30–09:30 shift. | Model validator rejects equal times. Seeded equal times still follow the overnight rule. |
| 6 | `create_employee` | Check-then-insert is race-prone. | Two parallel requests both pass the existence check. | Insert under unique `emp_code` index; translate duplicate-key failure into 409. |
| 7 | `create_employee` | Server creation timestamp is naive and returned as a datetime/ISO string. | Inspect `created_at`: the API requires integer epoch milliseconds. | UTC whole-second clock, BSON date storage and boundary serialization. |
| 8 | `list_employees` | `skip = page * page_size` skips the first page. | Page 1 of a one-employee database is empty. | `(page - 1) * page_size` in MongoDB. |
| 9 | `list_employees` | `total` ignores the department filter. | Filter a two-department database to one department. | Count the same predicate used for the page. |
| 10 | `list_employees` | No deterministic employee ordering. | Insert EMP0003 before EMP0001. | Sort by `emp_code` ascending before pagination. |
| 11 | Both list endpoints | No page or page-size bounds. | `page=0`, `page_size=-1`, `page_size=101`. | Shared query constraints enforce page >= 1 and size 1–100. |
| 12 | `PunchInIn` | Arbitrary statuses are accepted. | Punch in with `ABSENT` or an unknown status. | Presence-status literal enum. |
| 13 | `PunchInIn`, `punch_in` | Timestamps permit coercion, seconds-looking numbers and out-of-range inputs; truthiness conflates zero with omission. | Supply a numeric string, float, boolean, zero or seconds epoch. | Strict bounded integer milliseconds; omission uses a default factory, explicit invalid values return 422. |
| 14 | `punch_in` | Missing employee is dereferenced. | Unknown employee produces a server exception. | Shared employee lookup raises 404. |
| 15 | `punch_in` | Uses host-local date without the overnight attendance-day exception. | 01:00 IST on July 7 for a 22:00–06:00 shift must belong to July 6. | Convert to IST and apply previous-day assignment before storing the natural key. |
| 16 | `punch_in` | Milliseconds are not truncated before calculations or storage. | 09:40:00.900 must be stored as 09:40:00 and not count as late. | Epoch conversion floors to whole seconds first. |
| 17 | `compute_late_minutes` | Anchors the shift to the punch's timezone/calendar day, not the attendance day in IST. | Overnight 01:00 punch is incorrectly treated as before that day's 22:00 start. | Construct explicit IST shift bounds from the attendance date. |
| 18 | `compute_late_minutes` | Floors minutes before checking the strict ten-minute grace boundary. | 09:40:01 should be 10 late minutes; starter returns 0. | Compare elapsed seconds with 600 before flooring minutes. |
| 19 | `compute_work_hours` | Python `round`/binary float rounding does not implement decimal half-up. | 450 seconds = 0.125 hours must become 0.13. | Decimal division and `ROUND_HALF_UP`. |
| 20 | `compute_overtime` | Does not advance overnight shift end to the following date. | 22:00–06:00, out at 06:40 next day: expected 40 minutes. | Shift bounds advance the end by one day when end <= start. |
| 21 | `compute_overtime` | Omits the minimum 30-minute overtime threshold. | Out at 18:59:59 after an 18:30 shift must produce 0. | Floor elapsed minutes, then require at least 30. |
| 22 | `punch_in` | Attendance check-then-insert is race-prone. | Simultaneous same-day requests create duplicates. | Unique `(emp_code, date)` index and duplicate-key-to-409 handling. |
| 23 | Attendance responses | Invents an `id` from MongoDB `_id`, contrary to the natural-key contract. | Inspect punch-in and list responses. | No API record ID; internal `_id` is removed recursively. |
| 24 | Employee/attendance responses | Datetimes, including nested audit changes, are not converted to epoch milliseconds. | List seeded history containing BSON dates. | Recursive datetime serialization covers punches, creation, audit `at`, and before/after values. |
| 25 | `list_attendance` | Fetches and sorts every matching document in Python before slicing. | 100,000 rows cause unnecessary memory use and latency. | Indexed MongoDB sort, skip, limit and filtered count. |
| 26 | `list_attendance` | Sort lacks the secondary `emp_code` key. | Several employees share an attendance date. | Sort date descending, employee code ascending. |
| 27 | `list_attendance` | Invalid dates, reversed ranges and unknown status filters are accepted. | `date_from=2026-02-30`, from > to, `status=BAD`. | Calendar/range validation and status enum return 422. |
| 28 | `list_attendance` | Missing legacy history/half-day fields are not normalized. | Seed a record without those fields. | Response defaults: empty history, false half-day; aggregation expressions use equivalent defaults. |

The unfinished punch-out, correction, analytics and explain endpoints have also been implemented. Corrections recompute derived values and append one audit entry in a conditional atomic update. Punch-out uses the latest eligible punch-in, including already closed records, so repeat requests cannot accidentally close an older open record.

Things examined that were **not** defects:

- `load_dotenv()` already preserves real environment variables by default; the replacement makes `override=False` explicit.
- Identifying employees by `emp_code` is correct; switching attendance references to ObjectIds would violate the data model.
- String calendar dates support chronological comparisons when strictly validated as `YYYY-MM-DD`.
- Synchronous PyMongo in synchronous FastAPI route functions is valid: FastAPI executes them in its thread pool.
- Null work hours and an empty initial history are correct for an open punch-in. Ordinary punches must not create audit entries.
- The starter grace period correctly intends to count lateness from shift start rather than subtracting the grace period; the boundary implementation needed fixing.
