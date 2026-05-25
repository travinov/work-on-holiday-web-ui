# Admin Week Locks And Unified Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the separate administrator login with unified employee authentication, role-based admin access, superuser bootstrap, and explicit period locks for planning and actual-time entry.

**Architecture:** Keep the existing FastAPI + SQLite + Jinja architecture. Extend `app_employee_profile` into the source of truth for user role/status, add a dedicated period-lock table for `planning` and `actual`, and keep request status separate from lock status. Access control becomes role-based: all users authenticate through the employee path except the release-configured superuser.

**Tech Stack:** FastAPI, Jinja2 templates, SQLite, Python `unittest`, `fastapi.testclient.TestClient`.

---

## File Structure

- Modify `src/app_request_state.py`: schema migrations for user role/status fields and period locks.
- Modify `src/web_ui.py`: authentication helpers, role checks, lock checks, admin routes, request mutation guards.
- Modify `templates/home.html`: remove separate admin-token login card; keep entry points to employee login and role-aware admin area.
- Modify `templates/employee.html`: show admin menu only for users with `is_admin` or `is_superuser`; keep employee cabinet behavior.
- Modify `templates/index.html`: make admin dashboard role-gated and add period-lock controls.
- Modify `templates/admin_users.html`: add role/status controls and remove dependency on separate admin-token mode.
- Modify `templates/admin_requests.html`: show lock/status context for requests.
- Modify `tests/test_weekly_reporting_and_locks.py`: add coverage for unified auth, admin role assignment, user status changes, and period locks.
- Optional create `templates/superuser_login.html` only if keeping superuser login visually separate is clearer than adding it to `home.html`.

---

### Task 1: Extend Schema For Roles, User Status, And Period Locks

**Files:**
- Modify: `src/app_request_state.py`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Write failing schema test**

Add this test method to `WeeklyReportingAndLocksTest`:

```python
def test_app_tables_include_user_roles_status_and_period_locks(self) -> None:
    with sqlite3.connect(self.db_path) as conn:
        profile_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(app_employee_profile)").fetchall()
        }
        self.assertIn("is_admin", profile_columns)
        self.assertIn("is_superuser", profile_columns)
        self.assertIn("employee_status", profile_columns)
        self.assertIn("status_reason", profile_columns)
        self.assertIn("blocked_at", profile_columns)
        self.assertIn("archived_at", profile_columns)
        self.assertIn("restored_at", profile_columns)
        self.assertIn("updated_by", profile_columns)

        lock_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(app_period_lock)").fetchall()
        }
        self.assertEqual(
            {
                "lock_id",
                "lock_type",
                "date_from",
                "date_to",
                "created_by",
                "created_at",
                "comment",
            },
            lock_columns,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks.WeeklyReportingAndLocksTest.test_app_tables_include_user_roles_status_and_period_locks -v
```

Expected: FAIL because columns/table are missing.

- [ ] **Step 3: Implement schema migration**

In `src/app_request_state.py`, extend `ensure_app_tables` after `app_employee_profile` creation:

```python
    profile_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(app_employee_profile)").fetchall()
    }
    profile_column_defs = {
        "is_admin": "INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0, 1))",
        "is_superuser": "INTEGER NOT NULL DEFAULT 0 CHECK(is_superuser IN (0, 1))",
        "employee_status": "TEXT NOT NULL DEFAULT 'active' CHECK(employee_status IN ('active', 'blocked', 'archived'))",
        "status_reason": "TEXT",
        "blocked_at": "TEXT",
        "archived_at": "TEXT",
        "restored_at": "TEXT",
        "updated_by": "TEXT",
    }
    for column_name, column_def in profile_column_defs.items():
        if column_name not in profile_columns:
            conn.execute(f"ALTER TABLE app_employee_profile ADD COLUMN {column_name} {column_def};")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_period_lock (
            lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_type TEXT NOT NULL CHECK(lock_type IN ('planning', 'actual')),
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            comment TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_period_lock_type_dates
        ON app_period_lock (lock_type, date_from, date_to);
        """
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks.WeeklyReportingAndLocksTest.test_app_tables_include_user_roles_status_and_period_locks -v
```

Expected: PASS.

- [ ] **Step 5: Run full regression for schema safety**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks -v
```

Expected: all tests pass.

---

### Task 2: Add Unified Role Helpers And Superuser Bootstrap

**Files:**
- Modify: `src/web_ui.py`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Write failing tests for role resolution**

Add tests:

```python
def test_employee_with_admin_flag_can_open_admin_dashboard(self) -> None:
    insert_planned_request(
        self.db_path,
        response_id=901,
        full_name="Админов Андрей Иванович",
        full_name_key="админов андрей иванович",
        planned_date="2026-04-22",
    )
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_employee_profile (full_name_key, grade_12_plus, is_admin, updated_at)
            VALUES (?, 0, 1, '2026-04-20T10:00:00')
            """,
            ("админов андрей иванович",),
        )
        conn.commit()

    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        login = client.post("/employee/login", data={"full_name": "Админов Андрей Иванович"})
        self.assertEqual(200, login.status_code)
        admin_page = client.get("/admin")

    self.assertEqual(200, admin_page.status_code)
    self.assertIn("Кабинет администратора", admin_page.text)


def test_regular_employee_cannot_open_admin_dashboard(self) -> None:
    insert_planned_request(
        self.db_path,
        response_id=902,
        full_name="Рядовой Роман Иванович",
        full_name_key="рядовой роман иванович",
        planned_date="2026-04-22",
    )

    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        login = client.post("/employee/login", data={"full_name": "Рядовой Роман Иванович"})
        self.assertEqual(200, login.status_code)
        admin_page = client.get("/admin", follow_redirects=False)

    self.assertEqual(303, admin_page.status_code)
    self.assertIn("администратору", unquote(admin_page.headers["location"]).lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_weekly_reporting_and_locks.WeeklyReportingAndLocksTest.test_employee_with_admin_flag_can_open_admin_dashboard \
  tests.test_weekly_reporting_and_locks.WeeklyReportingAndLocksTest.test_regular_employee_cannot_open_admin_dashboard \
  -v
```

Expected: FAIL because `/admin` still uses the old admin cookie.

- [ ] **Step 3: Add role helpers**

In `src/web_ui.py`, add helpers near authentication helpers:

```python
SUPERUSER_LOGIN_ENV = "WORK_ON_HOLIDAY_SUPERUSER_LOGIN"
SUPERUSER_PASSWORD_ENV = "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD"
SUPERUSER_COOKIE_NAME = "woh_superuser"


def get_employee_profile_flags(conn: sqlite3.Connection, full_name_key: str) -> dict[str, Any]:
    profile = get_employee_profile(conn, full_name_key)
    return {
        "employee_key": full_name_key,
        "grade_12_plus": int(profile.get("grade_12_plus") or 0),
        "is_admin": int(profile.get("is_admin") or 0),
        "is_superuser": int(profile.get("is_superuser") or 0),
        "employee_status": profile.get("employee_status") or "active",
    }


def enrich_employee_session(conn: sqlite3.Connection, employee: dict[str, str]) -> dict[str, Any]:
    flags = get_employee_profile_flags(conn, employee["employee_key"])
    return {**employee, **flags}


def is_employee_admin(employee_session: dict[str, Any] | None) -> bool:
    return bool(employee_session and (employee_session.get("is_admin") or employee_session.get("is_superuser")))
```

Update `authenticate_employee_by_token` to return `enrich_employee_session(conn, employee)` instead of only key/name.

- [ ] **Step 4: Replace admin dashboard auth check**

In `/admin`, `/admin/users`, `/admin/requests`, report generation, download, and admin mutation routes, replace `is_admin_authenticated(request)` with:

```python
employee_session = authenticate_employee_by_token(request)
if not is_employee_admin(employee_session):
    return RedirectResponse(url="/?msg=Раздел доступен только администратору&level=error", status_code=303)
```

For routes returning `FileResponse`, raise 403 if no admin session.

- [ ] **Step 5: Run targeted tests**

Run the two tests from Step 2.

Expected: PASS.

- [ ] **Step 6: Keep old admin-token tests failing intentionally until Task 4**

Do not remove old login routes yet. They will be removed after superuser login is in place.

---

### Task 3: Add Superuser Login And Admin Role Assignment

**Files:**
- Modify: `src/web_ui.py`
- Modify: `templates/home.html`
- Modify: `templates/admin_users.html`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Write failing test for superuser role assignment**

Add:

```python
def test_superuser_can_assign_admin_role_to_employee(self) -> None:
    insert_planned_request(
        self.db_path,
        response_id=903,
        full_name="Назначаемый Николай Иванович",
        full_name_key="назначаемый николай иванович",
        planned_date="2026-04-22",
    )

    with (
        patch.dict(
            "os.environ",
            {
                "WORK_ON_HOLIDAY_SUPERUSER_LOGIN": "root",
                "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD": "release-password",
            },
        ),
        patch.object(web_ui, "DB_PATH", self.db_path),
    ):
        client = TestClient(web_ui.app)
        login = client.post(
            "/superuser/login",
            data={"login": "root", "password": "release-password"},
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        update = client.post(
            "/admin/employee/admin-role",
            data={"employee_key": "назначаемый николай иванович", "is_admin": "1"},
            follow_redirects=False,
        )
        self.assertEqual(303, update.status_code)

    with sqlite3.connect(self.db_path) as conn:
        is_admin = conn.execute(
            "SELECT is_admin FROM app_employee_profile WHERE full_name_key = ?",
            ("назначаемый николай иванович",),
        ).fetchone()[0]
    self.assertEqual(1, is_admin)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks.WeeklyReportingAndLocksTest.test_superuser_can_assign_admin_role_to_employee -v
```

Expected: FAIL because routes do not exist.

- [ ] **Step 3: Implement superuser auth**

Add to `src/web_ui.py`:

```python
def build_superuser_cookie_value(login: str, password: str) -> str:
    return hmac.new(password.encode("utf-8"), login.encode("utf-8"), hashlib.sha256).hexdigest()


def is_superuser_authenticated(request: Request) -> bool:
    login = os.getenv(SUPERUSER_LOGIN_ENV, "").strip()
    password = os.getenv(SUPERUSER_PASSWORD_ENV, "").strip()
    cookie_value = request.cookies.get(SUPERUSER_COOKIE_NAME, "")
    if not login or not password or not cookie_value:
        return False
    expected = build_superuser_cookie_value(login, password)
    return hmac.compare_digest(cookie_value, expected)


def is_admin_or_superuser_request(request: Request) -> bool:
    if is_superuser_authenticated(request):
        return True
    return is_employee_admin(authenticate_employee_by_token(request))
```

Add routes:

```python
@app.post("/superuser/login")
def superuser_login(login: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    configured_login = os.getenv(SUPERUSER_LOGIN_ENV, "").strip()
    configured_password = os.getenv(SUPERUSER_PASSWORD_ENV, "").strip()
    if not configured_login or not configured_password:
        return redirect_with_message("/", "Суперпользователь не настроен", "error")
    if login.strip() != configured_login or password.strip() != configured_password:
        return redirect_with_message("/", "Неверный логин или пароль суперпользователя", "error")
    response = RedirectResponse(url="/admin?msg=Суперпользователь вошел&level=success", status_code=303)
    response.set_cookie(
        SUPERUSER_COOKIE_NAME,
        build_superuser_cookie_value(configured_login, configured_password),
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/superuser/logout")
def superuser_logout() -> RedirectResponse:
    response = RedirectResponse(url="/?msg=Суперпользователь вышел&level=info", status_code=303)
    response.delete_cookie(SUPERUSER_COOKIE_NAME)
    return response
```

- [ ] **Step 4: Implement role assignment route**

Add:

```python
@app.post("/admin/employee/admin-role")
def admin_update_employee_admin_role(
    request: Request,
    employee_key: str = Form(...),
    is_admin: str = Form("0"),
) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Изменение роли доступно только администратору", "error")
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        if not get_employee_display_name(conn, employee_key):
            return redirect_with_message("/admin/users", "Сотрудник не найден", "error")
        profile = get_employee_profile(conn, employee_key)
        upsert_employee_grade_12_plus(conn, employee_key, bool(profile.get("grade_12_plus")))
        conn.execute(
            """
            UPDATE app_employee_profile
            SET is_admin = ?, updated_at = ?
            WHERE full_name_key = ? AND COALESCE(is_superuser, 0) = 0
            """,
            (1 if is_admin == "1" else 0, datetime.now().isoformat(timespec="seconds"), employee_key),
        )
        conn.commit()
    return redirect_with_message("/admin/users", "Роль пользователя обновлена", "success")
```

- [ ] **Step 5: Update UI**

In `templates/home.html`, replace old admin-token form with superuser login form:

```html
<form action="/superuser/login" method="post" class="token-form">
  <input type="text" name="login" placeholder="Логин суперпользователя" required />
  <input type="password" name="password" placeholder="Пароль суперпользователя" required />
  <button type="submit">Войти как суперпользователь</button>
</form>
```

In `templates/admin_users.html`, add admin role control near grade control:

```html
<form action="/admin/employee/admin-role" method="post" class="row">
  <input type="hidden" name="employee_key" value="{{ user.employee_key }}" />
  <input type="hidden" name="is_admin" value="0" />
  <label class="check-row">
    <input type="checkbox" name="is_admin" value="1" {% if user.is_admin %}checked{% endif %} />
    Администратор
  </label>
  <button type="submit">Сохранить роль</button>
</form>
```

- [ ] **Step 6: Run targeted test**

Run the test from Step 2.

Expected: PASS.

---

### Task 4: Remove Separate Administrator Token Login

**Files:**
- Modify: `src/web_ui.py`
- Modify: `templates/home.html`
- Modify: `templates/index.html`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Update tests that depend on `WORK_ON_HOLIDAY_ADMIN_TOKEN`**

Replace admin login setup in tests with either:

```python
client.post("/superuser/login", data={"login": "root", "password": "release-password"}, follow_redirects=False)
```

under:

```python
patch.dict(
    "os.environ",
    {
        "WORK_ON_HOLIDAY_SUPERUSER_LOGIN": "root",
        "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD": "release-password",
    },
)
```

or create an employee with `is_admin = 1` and log in via `/employee/login`.

- [ ] **Step 2: Add failing test that old admin login is gone**

```python
def test_legacy_admin_login_route_is_removed(self) -> None:
    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        response = client.post("/admin/login", data={"admin_token": "secret"})
    self.assertEqual(404, response.status_code)
```

- [ ] **Step 3: Remove old admin-token code**

Delete from `src/web_ui.py`:

```python
ADMIN_TOKEN_ENV = "WORK_ON_HOLIDAY_ADMIN_TOKEN"
ADMIN_COOKIE_NAME = "woh_admin"
def get_admin_token() -> str: ...
def admin_auth_configured() -> bool: ...
def build_admin_cookie_value(token: str) -> str: ...
def is_admin_authenticated(request: Request) -> bool: ...
@app.post("/admin/login") ...
@app.post("/admin/logout") ...
```

Replace any remaining `is_admin_authenticated(request)` calls with `is_admin_or_superuser_request(request)`.

- [ ] **Step 4: Remove old admin-token UI**

In `templates/home.html` and `templates/index.html`, remove fields named `admin_token` and text `Админ-токен`.

- [ ] **Step 5: Run tests**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks -v
```

Expected: all tests pass.

---

### Task 5: Implement Planning And Actual Period Locks

**Files:**
- Modify: `src/web_ui.py`
- Modify: `templates/index.html`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Write failing tests for lock behavior**

Add:

```python
def test_planning_lock_blocks_employee_create_correct_and_cancel(self) -> None:
    insert_planned_request(
        self.db_path,
        response_id=904,
        full_name="Локов Петр Иванович",
        full_name_key="локов петр иванович",
        planned_date="2026-04-22",
    )
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
            VALUES ('planning', '2026-04-20', '2026-04-26', 'admin', '2026-04-20T10:00:00', 'test')
            """
        )
        conn.commit()

    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        client.post("/employee/login", data={"full_name": "Локов Петр Иванович"})
        create_response = client.post(
            "/employee/request/create",
            data={
                "planned_work_date": "2026-04-22",
                "planned_work_time": "10:00 - 12:00",
                "payment_type": "Отгул",
                "task_description": "Blocked create",
                "justification": "Test",
                "systems": "System",
            },
            follow_redirects=False,
        )
        cancel_response = client.post(
            "/employee/request/cancel",
            data={"employee_key": "локов петр иванович", "response_id": "904"},
            follow_redirects=False,
        )

    self.assertEqual(303, create_response.status_code)
    self.assertIn("прием заявок закрыт", unquote(create_response.headers["location"]).lower())
    self.assertEqual(303, cancel_response.status_code)
    self.assertIn("прием заявок закрыт", unquote(cancel_response.headers["location"]).lower())


def test_actual_lock_blocks_employee_actual_time_by_planned_date(self) -> None:
    insert_planned_request(
        self.db_path,
        response_id=905,
        full_name="Фактов Петр Иванович",
        full_name_key="фактов петр иванович",
        planned_date="2026-04-22",
    )
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
            VALUES ('actual', '2026-04-20', '2026-04-26', 'admin', '2026-04-27T10:00:00', 'test')
            """
        )
        conn.commit()

    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        client.post("/employee/login", data={"full_name": "Фактов Петр Иванович"})
        response = client.post(
            "/employee/request/actual",
            data={
                "employee_key": "фактов петр иванович",
                "response_id": "905",
                "actual_work_date": "2026-04-23",
                "actual_work_time": "10:00 - 12:00",
            },
            follow_redirects=False,
        )

    self.assertEqual(303, response.status_code)
    self.assertIn("ввод факта закрыт", unquote(response.headers["location"]).lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run both tests by name.

Expected: FAIL because lock checks do not exist yet.

- [ ] **Step 3: Add period-lock helpers**

Add to `src/web_ui.py`:

```python
def is_period_locked(conn: sqlite3.Connection, lock_type: str, target_date: str) -> bool:
    row = conn.execute(
        """
        SELECT lock_id
        FROM app_period_lock
        WHERE lock_type = ?
          AND ? BETWEEN date_from AND date_to
        LIMIT 1;
        """,
        (lock_type, target_date),
    ).fetchone()
    return row is not None


def get_request_planned_date(conn: sqlite3.Connection, response_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT COALESCE(st.override_planned_work_date, r.planned_work_date) AS planned_date
        FROM survey_responses r
        LEFT JOIN app_request_state st ON st.response_id = r.response_id
        WHERE r.response_id = ?;
        """,
        (response_id,),
    ).fetchone()
    return row["planned_date"] if row else None
```

- [ ] **Step 4: Apply lock checks**

In `employee_create_request`, before insert:

```python
if not is_admin_mode and is_period_locked(conn, "planning", planned_work_date):
    return build_employee_redirect(
        employee_key,
        "Прием заявок закрыт для выбранной даты",
        "error",
        admin_mode=False,
    )
```

In `employee_cancel_request`, before state update:

```python
planned_date = get_request_planned_date(conn, response_id)
if not is_admin_mode and planned_date and is_period_locked(conn, "planning", planned_date):
    return build_employee_redirect(employee_key, "Прием заявок закрыт для этой заявки", "error", admin_mode=False)
```

In `employee_correct_request`, check current planned date and new planned date:

```python
current_planned_date = get_request_planned_date(conn, response_id)
if not is_admin_mode and current_planned_date and is_period_locked(conn, "planning", current_planned_date):
    return build_employee_redirect(employee_key, "Прием заявок закрыт для этой заявки", "error", admin_mode=False)
if not is_admin_mode and planned_work_date and is_period_locked(conn, "planning", planned_work_date):
    return build_employee_redirect(employee_key, "Прием заявок закрыт для выбранной даты", "error", admin_mode=False)
```

In `employee_set_actual_time`, check by planned date of the request:

```python
planned_date = get_request_planned_date(conn, response_id)
if not is_admin_mode and planned_date and is_period_locked(conn, "actual", planned_date):
    return build_employee_redirect(employee_key, "Ввод факта закрыт для этой заявки", "error", admin_mode=False)
```

- [ ] **Step 5: Add admin routes to create locks**

Add:

```python
@app.post("/admin/locks/create")
def admin_create_period_lock(
    request: Request,
    lock_type: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    comment: str = Form(""),
) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Закрытие периода доступно только администратору", "error")
    if lock_type not in {"planning", "actual"}:
        return redirect_with_message("/admin", "Некорректный тип закрытия", "error")
    parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    if parsed_from > parsed_to:
        return redirect_with_message("/admin", "Дата начала позже даты окончания", "error")
    actor = "superuser" if is_superuser_authenticated(request) else authenticate_employee_by_token(request)["employee_key"]
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        conn.execute(
            """
            INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lock_type, date_from, date_to, actor, datetime.now().isoformat(timespec="seconds"), comment.strip() or None),
        )
        conn.commit()
    label = "Прием заявок закрыт" if lock_type == "planning" else "Ввод факта закрыт"
    return redirect_with_message("/admin", f"{label}: {date_from} - {date_to}", "success")
```

- [ ] **Step 6: Add admin UI controls**

In `templates/index.html`, add card:

```html
<div class="card">
  <h3>Закрытие периодов</h3>
  <form action="/admin/locks/create" method="post">
    <div class="row">
      <select name="lock_type" required>
        <option value="planning">Закрыть прием заявок</option>
        <option value="actual">Закрыть ввод факта</option>
      </select>
      <input type="date" name="date_from" value="{{ default_week_from }}" required />
      <input type="date" name="date_to" value="{{ default_week_to }}" required />
      <input type="text" name="comment" placeholder="Комментарий" />
      <button type="submit">Закрыть период</button>
    </div>
  </form>
</div>
```

- [ ] **Step 7: Run tests**

Run targeted lock tests, then full suite:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks -v
```

Expected: all tests pass.

---

### Task 6: Add User Status Management And Request Handling Rules

**Files:**
- Modify: `src/web_ui.py`
- Modify: `templates/admin_users.html`
- Test: `tests/test_weekly_reporting_and_locks.py`

- [ ] **Step 1: Write tests for block/archive/restore**

Add tests covering:

```python
def test_blocked_employee_cannot_login(self) -> None:
    insert_planned_request(self.db_path, response_id=906, full_name="Блоков Иван Иванович", full_name_key="блоков иван иванович", planned_date="2026-04-22")
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_employee_profile (full_name_key, grade_12_plus, employee_status, updated_at)
            VALUES (?, 0, 'blocked', '2026-04-20T10:00:00')
            """,
            ("блоков иван иванович",),
        )
        conn.commit()
    with patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        response = client.post("/employee/login", data={"full_name": "Блоков Иван Иванович"}, follow_redirects=False)
    self.assertEqual(303, response.status_code)
    self.assertIn("заблокирован", unquote(response.headers["location"]).lower())
```

Add archive test:

```python
def test_archiving_user_cancels_only_unlocked_active_requests(self) -> None:
    insert_planned_request(self.db_path, response_id=907, full_name="Архивов Иван Иванович", full_name_key="архивов иван иванович", planned_date="2026-04-22")
    with patch.dict("os.environ", {"WORK_ON_HOLIDAY_SUPERUSER_LOGIN": "root", "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD": "release-password"}), patch.object(web_ui, "DB_PATH", self.db_path):
        client = TestClient(web_ui.app)
        client.post("/superuser/login", data={"login": "root", "password": "release-password"}, follow_redirects=False)
        response = client.post("/admin/employee/status", data={"employee_key": "архивов иван иванович", "employee_status": "archived", "status_reason": "left"}, follow_redirects=False)
    self.assertEqual(303, response.status_code)
    with sqlite3.connect(self.db_path) as conn:
        status = conn.execute("SELECT status FROM app_request_state WHERE response_id = 907").fetchone()[0]
    self.assertEqual("cancelled", status)
```

- [ ] **Step 2: Implement status update route**

Add:

```python
@app.post("/admin/employee/status")
def admin_update_employee_status(
    request: Request,
    employee_key: str = Form(...),
    employee_status: str = Form(...),
    status_reason: str = Form(""),
) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Изменение статуса доступно только администратору", "error")
    if employee_status not in {"active", "blocked", "archived"}:
        return redirect_with_message("/admin/users", "Некорректный статус пользователя", "error")
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        if not get_employee_display_name(conn, employee_key):
            return redirect_with_message("/admin/users", "Сотрудник не найден", "error")
        profile = get_employee_profile(conn, employee_key)
        upsert_employee_grade_12_plus(conn, employee_key, bool(profile.get("grade_12_plus")))
        conn.execute(
            """
            UPDATE app_employee_profile
            SET employee_status = ?,
                status_reason = ?,
                blocked_at = CASE WHEN ? = 'blocked' THEN ? ELSE blocked_at END,
                archived_at = CASE WHEN ? = 'archived' THEN ? ELSE archived_at END,
                restored_at = CASE WHEN ? = 'active' THEN ? ELSE restored_at END,
                updated_at = ?
            WHERE full_name_key = ?
            """,
            (employee_status, status_reason.strip() or None, employee_status, now, employee_status, now, employee_status, now, now, employee_key),
        )
        if employee_status == "archived":
            rows = conn.execute(
                """
                SELECT r.response_id
                FROM survey_responses r
                LEFT JOIN app_request_state st ON st.response_id = r.response_id
                WHERE r.full_name_key = ?
                  AND r.request_type = 'Подать заявку'
                  AND COALESCE(st.status, 'active') = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM app_period_lock pl
                    WHERE pl.lock_type = 'planning'
                      AND COALESCE(st.override_planned_work_date, r.planned_work_date) BETWEEN pl.date_from AND pl.date_to
                  )
                """,
                (employee_key,),
            ).fetchall()
            for row in rows:
                request_uid = f"req:{row['response_id']}"
                upsert_request_state(
                    conn,
                    request_uid=request_uid,
                    response_id=int(row["response_id"]),
                    full_name_key=employee_key,
                    updates={"status": "cancelled"},
                )
        conn.commit()
    return redirect_with_message("/admin/users", "Статус пользователя обновлен", "success")
```

- [ ] **Step 3: Block login for blocked/archived users**

In `employee_login`, after resolving employee and profile:

```python
profile = get_employee_profile(conn, employee["employee_key"])
if profile.get("employee_status") == "blocked":
    return redirect_with_message("/employee", "Пользователь заблокирован", "error")
if profile.get("employee_status") == "archived":
    return redirect_with_message("/employee", "Пользователь архивирован", "error")
```

- [ ] **Step 4: Add admin UI status controls**

In `templates/admin_users.html`, add per-user form:

```html
<form action="/admin/employee/status" method="post" class="row">
  <input type="hidden" name="employee_key" value="{{ user.employee_key }}" />
  <select name="employee_status">
    <option value="active" {% if user.employee_status == 'active' %}selected{% endif %}>Активен</option>
    <option value="blocked" {% if user.employee_status == 'blocked' %}selected{% endif %}>Заблокирован</option>
    <option value="archived" {% if user.employee_status == 'archived' %}selected{% endif %}>Архивирован</option>
  </select>
  <input type="text" name="status_reason" value="{{ user.status_reason or '' }}" placeholder="Причина" />
  <button type="submit">Сохранить статус</button>
</form>
```

- [ ] **Step 5: Run tests**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks -v
```

Expected: all tests pass.

---

### Task 7: Final Cleanup And Rendered Verification

**Files:**
- Modify only if tests/rendered checks expose defects.

- [ ] **Step 1: Compile Python**

Run:

```bash
venv/bin/python -m py_compile src/app_request_state.py src/web_ui.py tests/test_weekly_reporting_and_locks.py
```

Expected: no output, exit code 0.

- [ ] **Step 2: Run full tests**

Run:

```bash
venv/bin/python -m unittest tests.test_weekly_reporting_and_locks -v
```

Expected: all tests pass.

- [ ] **Step 3: Restart local server**

Run:

```bash
if lsof -nP -iTCP:8080 -sTCP:LISTEN >/tmp/woh_lsof.txt 2>/dev/null; then awk 'NR>1 {print $2}' /tmp/woh_lsof.txt | xargs -r kill; fi
WORK_ON_HOLIDAY_SUPERUSER_LOGIN='root' WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='release-password' venv/bin/python -m uvicorn src.web_ui:app --host 127.0.0.1 --port 8080
```

Expected: `Uvicorn running on http://127.0.0.1:8080`.

- [ ] **Step 4: Smoke-check HTML**

Run in a second shell:

```bash
curl -sS -o /tmp/woh_home.html -w 'home=%{http_code}\n' http://127.0.0.1:8080/
rg -n "Админ-токен|superuser|суперпользователь|Кабинет сотрудника" /tmp/woh_home.html
```

Expected:
- `home=200`
- no `Админ-токен`
- superuser login visible if configured.

- [ ] **Step 5: Manual UI checks**

Open `http://127.0.0.1:8080/` and verify:

- employee login still works;
- superuser login works;
- superuser can open `/admin/users`;
- admin role checkbox is visible;
- period-lock form is visible in `/admin`;
- regular employee cannot open admin pages;
- planning lock blocks employee create/correct/cancel;
- actual lock blocks employee actual-time entry.

---

## Self-Review

**Spec coverage:**
- Unified employee/admin auth: Tasks 2-4.
- Superuser release-configured login/password: Task 3.
- Admin can assign admin role: Task 3.
- Remove separate admin login: Task 4.
- Close planning period: Task 5.
- Close actual period by previously created requests' planned dates: Task 5.
- User status management and request rules: Task 6.

**Placeholder scan:** No TBD/TODO placeholders are present. Each implementation task includes concrete files, tests, commands, and expected outcomes.

**Type consistency:** The plan consistently uses `app_employee_profile`, `app_period_lock`, `lock_type in ('planning', 'actual')`, `employee_status in ('active', 'blocked', 'archived')`, and existing request statuses `active/cancelled/completed`.
