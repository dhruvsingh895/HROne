# HROne — Employee Attendance & Analytics API

FastAPI and MongoDB API with punch-in/out, audited corrections, attendance analytics, and query explain endpoints. Requires Python 3.11+ and MongoDB 6.0+.

## Run

Start MongoDB separately, then run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = 'mongodb://127.0.0.1:27017'
$env:MONGO_DB = 'attendance_db'
uvicorn app.main:app --port 8000
```

On macOS/Linux, activate with `source .venv/bin/activate` and set variables with `export`. Real environment variables take precedence over local configuration. Indexes are created at startup. Readiness: `/health`; interactive API docs: `/docs`; API contract: `openapi.yaml`.

## Test

```powershell
python -m pip install -r requirements-dev.txt
$env:TEST_MONGO_URI = 'mongodb://127.0.0.1:27017'
python -m pytest -q
python tests/check_scale.py
```

Tests use real MongoDB, create randomly named test databases, and remove them afterward. The scale check generates 100,000 records and verifies indexed plans and analytics against independent calculations.

All application code is in `app/main.py`. See `REVIEW.md`, `DECISIONS.md`, and `VALIDATION.md` for the review, design choices, and results. Verified: 50 tests pass on MongoDB 8.3.4; MongoDB 7.0 has not been runtime-tested.
