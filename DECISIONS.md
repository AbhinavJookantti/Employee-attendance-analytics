# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code`, `(department, emp_code)` for the filtered list, and `joined_on` plus `(department, joined_on)` because the summary and trend start from employees. Attendance: unique `(emp_code, date)` (one record per day, monthly lookups), `(date desc, emp_code)` for the list and leaderboard, `(date desc, status, emp_code)` for the status filter. Rejected: `(date asc, status, emp_code)`, since the list sorts date descending and would need an in-memory sort.

2. **Punch-in race.** Both requests work out the same date and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one. The other gets `DuplicateKeyError`, which I return as 409. I never read first.

3. **Ties.** I rank with `$rank`, so equal totals share a rank and the next rank is skipped. I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned, even if that is more rows than `limit`.

4. **Headcount.** The summary starts from `employees` and uses `$lookup` for each person's logs. `$unwind` keeps empty results, so someone with no logs still counts as one.

5. **At 100x.** I would keep monthly totals per employee in a separate collection, updated on every write, so analytics stop reading raw logs.