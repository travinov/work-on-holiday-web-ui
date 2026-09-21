"""Manager read-only views and administrator roster workflow."""
import csv
import io
import json
import secrets
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import Form, HTTPException, Request, UploadFile, File
from fastapi.responses import RedirectResponse, Response

try:
    from src import team_directory as directory
except ModuleNotFoundError:
    import team_directory as directory


def register_routes(ui):
    app = ui.app

    def admin(request):
        session = ui.get_admin_session(request)
        if not session:
            raise HTTPException(403, 'Требуются права администратора')
        return session['employee_key']

    def manager(request, conn):
        session = ui.authenticate_employee_by_token(request)
        if not session or not directory.team_keys(conn, session['employee_key']):
            raise HTTPException(403, 'Требуется действующий доступ руководителя')
        return session['employee_key']

    def render(request, template, **context):
        return ui.templates.TemplateResponse(request, template, context)

    def draft(conn, identifier, actor):
        row = conn.execute("SELECT * FROM app_roster_draft WHERE id=? AND actor=? AND created_at > datetime('now','-1 day')", (identifier, actor)).fetchone()
        if not row:
            raise HTTPException(404, 'Предпросмотр не найден или истёк. Загрузите файл заново.')
        return row

    def preview(request, conn, identifier, actor):
        row = draft(conn, identifier, actor)
        plan = directory.plan_import(conn, json.loads(row['payload']), json.loads(row['bindings']))
        return render(request, 'roster.html', draft_id=identifier, plan=plan, people=directory.employees(conn))

    @app.get('/admin/roster')
    def roster(request: Request):
        admin(request)
        return render(request, 'roster.html', plan=None)

    @app.get('/admin/roster/template')
    def roster_template(request: Request, format: str = 'csv'):
        admin(request)
        rows = [directory.HEADERS, ['Иванов Иван Иванович', 'ivanov@example.org', ''],
                ['Петров Пётр Петрович', 'petrov@example.org', 'ivanov@example.org']]
        if format == 'xlsx':
            from openpyxl import Workbook
            book = Workbook()
            for row in rows:
                book.active.append(row)
            stream = io.BytesIO()
            book.save(stream)
            content = stream.getvalue()
            media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            format = 'csv'
            stream = io.StringIO()
            csv.writer(stream, delimiter=';').writerows(rows)
            content = stream.getvalue().encode('utf-8-sig')
            media = 'text/csv'
        return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="roster-template.{format}"'})

    @app.post('/admin/roster/preview')
    async def roster_preview(request: Request, file: UploadFile = File(...)):
        actor = admin(request)
        try:
            rows = directory.parse_roster(file.filename or '', await file.read(5 * 1024 * 1024 + 1))
        except Exception as exc:
            return render(request, 'roster.html', plan=None, error=f'Не удалось прочитать реестр: {exc}')
        identifier = secrets.token_urlsafe(24)
        with ui.get_db_connection() as conn:
            conn.execute("DELETE FROM app_roster_draft WHERE created_at <= datetime('now','-1 day')")
            conn.execute('INSERT INTO app_roster_draft(id,actor,payload,bindings,fingerprint) VALUES (?,?,?,?,?)',
                         (identifier, actor, json.dumps(rows), '{}', directory.fingerprint(conn)))
            return preview(request, conn, identifier, actor)

    @app.post('/admin/roster/{identifier}/resolve')
    async def roster_resolve(request: Request, identifier: str):
        actor = admin(request)
        form = await request.form()
        with ui.get_db_connection() as conn:
            row = draft(conn, identifier, actor)
            bindings = json.loads(row['bindings'])
            bindings.update({k[5:]: str(v) for k, v in form.items() if k.startswith('bind:') and v})
            conn.execute('UPDATE app_roster_draft SET bindings=?,fingerprint=? WHERE id=?',
                         (json.dumps(bindings), directory.fingerprint(conn), identifier))
            return preview(request, conn, identifier, actor)

    @app.post('/admin/roster/{identifier}/apply')
    def roster_apply(request: Request, identifier: str):
        actor = admin(request)
        with ui.get_db_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = draft(conn, identifier, actor)
            if row['fingerprint'] != directory.fingerprint(conn):
                raise HTTPException(409, 'Справочник изменился после предпросмотра. Загрузите реестр заново.')
            plan = directory.plan_import(conn, json.loads(row['payload']), json.loads(row['bindings']))
            if plan['errors'] or plan['unresolved']:
                raise HTTPException(422, 'Устраните ошибки в предпросмотре')
            directory.apply_import(conn, plan)
            conn.execute('DELETE FROM app_roster_draft WHERE id=?', (identifier,))
        return ui.redirect_with_message('/admin/users', f"Реестр применён: {len(plan['changes'])} сотрудников", 'success')

    def read_filter(request, conn, owner):
        query = request.query_params
        if query.get('saved'):
            row = conn.execute('SELECT payload FROM app_team_filter WHERE owner_key=? AND name=?', (owner, query['saved'])).fetchone()
            if not row:
                raise HTTPException(404, 'Подборка не найдена')
            raw = json.loads(row['payload'])
        elif query.get('apply'):
            raw = dict(query)
            raw['employees'] = query.getlist('employees')
        else:
            row = conn.execute("SELECT payload FROM app_team_filter WHERE owner_key=? AND name=''", (owner,)).fetchone()
            raw = json.loads(row['payload']) if row else {}
        try:
            filters = directory.normalize_filters(raw)
            if query.get('step') in {'-1', '1'}:
                start, _ = directory.filter_dates(filters)
                start = date.fromisoformat(start) + timedelta(days=7 * int(query['step']))
                filters.update(period='week', date_from=start.isoformat())
                filters = directory.normalize_filters(filters)
            if query.get('current'):
                filters['period'] = 'current'
                filters = directory.normalize_filters(filters)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, 'Некорректные фильтры: ' + str(exc))
        # Stored keys never expand current access. Keep stale selections so a removed
        # selection does not silently turn into "all employees".
        conn.execute('''INSERT INTO app_team_filter VALUES (?, '', ?) ON CONFLICT(owner_key,name)
                        DO UPDATE SET payload=excluded.payload''', (owner, json.dumps(filters)))
        return filters

    @app.get('/manager')
    def manager_page(request: Request):
        with ui.get_db_connection() as conn:
            owner = manager(request, conn)
            filters = read_filter(request, conn, owner)
            allowed = directory.team_keys(conn, owner, filters['scope'])
            people = directory.employees(conn)
            saved = [r[0] for r in conn.execute("SELECT name FROM app_team_filter WHERE owner_key=? AND name != '' ORDER BY name", (owner,))]
            start, end = directory.filter_dates(filters)
            selected = set(filters['employees'])
            team = sorted([people[k] for k in allowed if k in people], key=lambda p: p['name'])
            visible = [p for p in team if (not selected or p['key'] in selected)
                       and directory.name_key(filters['surname']) in directory.name_key(p['name'])]
            tasks, without = [], []
            for person in visible:
                items = [r for r in ui.get_employee_requests(person['key']) if start <= r['planned_work_date_iso'] <= end]
                if not items:
                    without.append(person)
                for item in items:
                    if filters['text'].casefold() not in item['task_description'].casefold():
                        continue
                    lead = people.get(person['manager'], {})
                    item['manager_name'] = lead.get('name', 'Без руководителя') + (' · ' + lead['email'] if lead.get('email') else '')
                    item['full_name'] = person['name']
                    tasks.append(item)
            tasks.sort(key=lambda r: (r['manager_name'] if filters['scope'] == 'all' else '', r['full_name'], r['planned_work_date_iso']))
        return render(request, 'manager.html', filters=filters, team=team, tasks=tasks, without=without,
                      date_from=start, date_to=end, saved=saved, stale=bool(selected - allowed))

    @app.post('/manager/filters')
    async def save_filter(request: Request):
        form = await request.form()
        name = str(form.get('name', '')).strip()
        if not name or len(name) > 80:
            raise HTTPException(422, 'Название подборки: от 1 до 80 символов')
        with ui.get_db_connection() as conn:
            owner = manager(request, conn)
            try:
                raw = dict(form)
                raw['employees'] = form.getlist('employees')
                filters = directory.normalize_filters(raw)
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc))
            conn.execute('''INSERT INTO app_team_filter VALUES (?,?,?) ON CONFLICT(owner_key,name)
                            DO UPDATE SET payload=excluded.payload''', (owner, name, json.dumps(filters)))
        return RedirectResponse('/manager?' + urlencode({'saved': name}), status_code=303)

    @app.post('/manager/filters/delete')
    def delete_filter(request: Request, name: str = Form(...)):
        with ui.get_db_connection() as conn:
            owner = manager(request, conn)
            if name:
                conn.execute('DELETE FROM app_team_filter WHERE owner_key=? AND name=?', (owner, name))
        return RedirectResponse('/manager', status_code=303)

    @app.get('/manager/requests/{response_id}')
    def manager_detail(request: Request, response_id: int):
        with ui.get_db_connection() as conn:
            owner = manager(request, conn)
            allowed = directory.team_keys(conn, owner, 'all')
            row = conn.execute("SELECT full_name_key FROM survey_responses WHERE response_id=? AND request_type='Подать заявку'", (response_id,)).fetchone()
            if not row or row[0] not in allowed:
                raise HTTPException(404, 'Задача не найдена')
            item = next((r for r in ui.get_employee_requests(row[0]) if r['response_id'] == response_id), None)
            if not item:
                raise HTTPException(404, 'Задача не найдена')
            item['full_name'] = directory.employees(conn).get(row[0], {}).get('name', item['full_name'])
        return render(request, 'manager_detail.html', task=item)
