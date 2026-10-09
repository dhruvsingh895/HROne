# Design decisions

1. **Indexes.** Unique employee-code and employee/date indexes enforce identity and one record per attendance day. Department/code supports employee listing; joined/department and department/joined/code support headcounts and trend lookups. Date/employee and status/date/employee support attendance ordering and monthly scans. Employee/punch-in finds the latest eligible punch. I rejected a standalone status index: low selectivity and no support for the required ordering.

2. **Punch-in race.** Both requests validate and compute their attendance date, then attempt insertion. MongoDB's unique employee/date index accepts exactly one insert. The other raises DuplicateKeyError, translated to 409. There is no correctness dependency on an earlier existence check.

3. **Ties.** MongoDB ranks total late minutes using `$rank` before filtering rank <= limit. Every tied employee at the cutoff is returned, ordered by employee code. Competition ranks skip subsequent positions.

4. **Headcount.** Summary starts from employees whose join date is within the period. Each employee looks up aggregated logs; an empty lookup contributes zero attendance but still one headcount.

5. **100x growth.** I would maintain incremental daily department summaries, invalidating affected days after corrections. This would reduce repeated joins while preserving the original attendance records and audit trail as the source of truth.
