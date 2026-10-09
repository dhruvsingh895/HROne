# Validation results

Verified on 9 October 2026 in the project folder, using Python 3.14.2, FastAPI 0.143.0, PyMongo 4.18.3 and a real MongoDB 8.3.4 instance bound to localhost on an isolated test port.

- **50 integration tests passed** in 3.39 seconds. Coverage includes input validation, IST/overnight boundaries, timestamp truncation, half-up rounding, pagination, legacy defaults, corrections, nested audit serialization, parallel employee creation/punches/corrections, analytics, and explain plans.
- The scale check inserted **100,000 attendance records and 1,050 employees before application startup**. Readiness, including index creation, completed in **2.022 seconds**.
- All five scale explain plans contained **IXSCAN** and no **COLLSCAN**. Department summary, tied leaderboard and the complete daily trend matched independent Python calculations over the generated data.
- The exact command `uvicorn app.main:app --port 8000` reached readiness in **3.287 seconds** in a separate process. HTTP health, generated OpenAPI (11 paths / 12 operations), and Swagger UI passed.

| Query on the scale dataset | Observed response time |
|---|---:|
| Filtered, paginated attendance | 0.017 s |
| Employee monthly summary | 0.019 s |
| Department summary | 1.017 s |
| Late leaderboard | 0.254 s |
| Department trend, 31 days | 2.045 s |

These are individual local observations, not a throughput benchmark or latency guarantee. The generated data includes employees with no logs, join-date changes, weekends, ties and half days. The integration suite separately checks absent records, missing weekdays, entirely empty log ranges and zero headcounts.

Limitations: the grader's hidden dataset and MongoDB 7.0 were not available for execution. The implementation uses aggregation operators available in MongoDB 6.0+, but runtime compatibility was exercised on 8.3.4. Installed Starlette emits one deprecation warning about its HTTPX test transport; all tests pass.
