# Work on Holiday Web UI

Приложение для оформления заявок на работу в выходной день, фиксации фактически отработанного времени и формирования Excel-отчетов для администратора.

Текущая версия работает без загрузки Excel/CSV-выгрузок из опроса. Источник данных — заявки, созданные пользователями через Web UI, и справочник сотрудников в локальной SQLite БД.

## Основной процесс

1. Администратор инициализирует БД и справочник сотрудников.
2. Сотрудник входит по ФИО. При первом входе получает персональный токен.
3. Сотрудник создает одну или несколько заявок на выход в выходной.
4. Администратор формирует общий Excel за выбранную неделю.
5. После работы сотрудник указывает фактическую дату и фактически отработанное время.
6. Администратор формирует отчетность по факту и при необходимости закрывает период.

## Структура БД

### `app_employee_directory`

Справочник сотрудников для Web-only сценария:

- `full_name_key` — нормализованный ключ ФИО;
- `full_name` — ФИО;
- `work_email`, `local_phone`, `mobile_phone`;
- `position_short_name`;
- `grade_num`;
- `created_at`, `updated_at`.

### `survey_responses`

Рабочая таблица заявок и фактов. Название сохранено для совместимости с существующими отчетами.

Ключевые поля:

- `response_id`;
- `full_name`, `full_name_normalized`, `full_name_key`;
- `request_type`;
- `grade_12_plus`, `payment_type`;
- `task_description`, `justification`;
- `planned_work_date`, `planned_work_time`;
- `actual_work_date`, `actual_work_time`;
- `target_work_date`;
- `system_1..system_6`;
- `row_hash`, `source_file`, `loaded_at`.

### `response_systems`

Нормализованный список АС по заявке:

- `response_id`;
- `system_order`;
- `system_name`.

### Прикладные таблицы

Создаются автоматически:

- `app_employee_auth` — токены сотрудников;
- `app_employee_profile` — грейд 12+, роль администратора, статус пользователя;
- `app_request_state` — статус заявки, корректировки, факт;
- `app_report_lock` — фиксация заявок после формирования отчета;
- `app_period_lock` — закрытие приема заявок или ввода факта по периоду.

## Инициализация БД

```bash
venv/bin/python src/init_db.py --db survey_results.db
```

Скрипт создает пустую Web-only БД со всеми таблицами, необходимыми для работы приложения.

## Генерация пользователей

Создать 10 тестовых сотрудников:

```bash
venv/bin/python src/generate_users.py --db survey_results.db --count 10
```

Создать повторяемый набор и предварительно очистить справочник пользователей, профили и токены:

```bash
venv/bin/python src/generate_users.py \
  --db survey_results.db \
  --count 10 \
  --seed 42 \
  --overwrite
```

Скрипт заполняет `app_employee_directory` и `app_employee_profile`. Заявки при этом не создаются.

## Web UI

Установка зависимостей:

```bash
venv/bin/pip install -r requirements.txt
```

Локальный запуск:

```bash
export WORK_ON_HOLIDAY_SUPERUSER_LOGIN='root'
export WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='release-password'
venv/bin/python -m uvicorn src.web_ui:app --host 127.0.0.1 --port 8080
```

Открыть:

```text
http://127.0.0.1:8080/
```

В интерфейсе доступны:

- кабинет сотрудника;
- создание и корректировка заявок;
- ввод фактически отработанного времени;
- отмена заявки;
- кабинет администратора;
- управление пользователями, грейдом 12+, ролью администратора и статусом сотрудника;
- генерация тестовых заявок на отдельной админ-странице;
- закрытие приема заявок и закрытие ввода факта по периоду;
- формирование общего Excel-файла с отчетами 1-4 за выбранную неделю.

## Развертывание на сервере

### Рекомендуемая схема через ZIP

Схема развертывания:

1. Скачать ZIP из GitHub.
2. Распаковать ZIP и обновить проектную папку на сервере.
3. Открыть терминал в проектной папке.
4. Запустить deploy-скрипт.

ZIP:

```text
https://codeload.github.com/travinov/work-on-holiday-web-ui/zip/refs/heads/main
```

Первое развертывание:

```bash
sudo WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-first-deploy' \
  deploy/scripts/deploy-corporate-server.sh
```

Повторное развертывание:

```bash
sudo deploy/scripts/deploy-corporate-server.sh
```

Скрипт использует production-настройки:

- приложение запускается из текущей проектной папки;
- БД хранится вне папки проекта: `/var/lib/work-on-holiday/survey_results.db`;
- backup БД хранится вне папки проекта: `/var/backups/work-on-holiday`;
- приложение запускается на `127.0.0.1:8081`;
- при повторном развертывании БД не перетирается, перед обновлением создается backup.

Важно: рабочую БД нельзя хранить внутри папки, которая заменяется содержимым ZIP. Поэтому production-скрипт выносит БД в `/var/lib/work-on-holiday`.

Проверка после deploy:

```bash
curl -I http://127.0.0.1:8081/
systemctl status work-on-holiday.service
```

### Альтернатива: развертывание с локальной машины на корпоративный сервер

С локальной машины проект отправляется на сервер через SSH/rsync, а затем на сервере запускается безопасный deploy с backup БД:

```bash
DEPLOY_HOST=tsles-assai0001.esrt.sber.ru \
DEPLOY_USER=CI09479675-lnx-travinov \
DEPLOY_PATH=/opt/work-on-holiday \
REMOTE_DB_PATH=/var/lib/work-on-holiday/survey_results.db \
REMOTE_BACKUP_DIR=/var/backups/work-on-holiday \
REMOTE_PORT=8081 \
WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-first-deploy' \
deploy/scripts/deploy-remote.sh
```

Эту команду нужно запускать с корпоративной машины, где есть SSH-доступ к `tsles-assai0001.esrt.sber.ru`.

Remote-скрипт не копирует локальные данные:

- `survey_results.db`;
- `reports/`;
- `generated_exports/`;
- `restore_points/`;
- `backups/`;
- `venv/`.

Серверная БД остается на сервере. При повторном deploy перед обновлением схемы создается backup серверной БД.

### Базовый серверный скрипт

Если проект уже находится на сервере, основной скрипт:

```bash
sudo WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-server' deploy/scripts/deploy-server.sh
```

Что делает скрипт:

- создает `venv`, если его еще нет;
- устанавливает зависимости из `requirements.txt`;
- при первом развертывании создает `survey_results.db`;
- при повторном развертывании сначала делает backup существующей БД в `backups/`;
- применяет инициализацию/обновление схемы без удаления данных;
- создает systemd service;
- запускает или перезапускает приложение.

Пример с явными путями:

```bash
sudo APP_DIR=/opt/work-on-holiday \
  DB_PATH=/var/lib/work-on-holiday/survey_results.db \
  BACKUP_DIR=/var/backups/work-on-holiday \
  PORT=8081 \
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-server' \
  deploy/scripts/deploy-server.sh
```

Для проверки без установки systemd:

```bash
SKIP_SYSTEMD=1 WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='local-test' deploy/scripts/deploy-server.sh
```

HTTPS с self-signed сертификатом описан в `deploy/HTTPS_SELF_SIGNED.md`.

## Отчеты

Общий Excel-файл:

```bash
venv/bin/python src/build_weekend_reports.py \
  --db survey_results.db \
  --date-from 2026-04-20 \
  --date-to 2026-04-26 \
  --employees-csv data/employees_mock.csv
```

Результат:

- файл `Отчеты выхода выходные <период>.xlsx`;
- лист `Отчет 1` — отчет для руководства по плановым заявкам;
- лист `Отчет 2` — краткий отчет для заведения заявок;
- лист `Отчет 3` — фактически отработанное время;
- лист `Отчет 4` — сверка заявок и факта.

Отдельные отчеты также можно запускать напрямую:

```bash
venv/bin/python src/report_first_management.py --db survey_results.db --date-from 2026-04-20 --date-to 2026-04-26
venv/bin/python src/report_second_requests.py --db survey_results.db --date-from 2026-04-20 --date-to 2026-04-26
venv/bin/python src/report_third_closure.py --db survey_results.db --date-from 2026-04-20 --date-to 2026-04-26
venv/bin/python src/report_four_reconciliation.py --db survey_results.db --date-from 2026-04-20 --date-to 2026-04-26
```

## Тесты

```bash
venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## Локальные данные

Не коммитить:

- `survey_results.db`;
- `reports/`;
- `generated_exports/`;
- `restore_points/`;
- реальные Excel/CSV с персональными данными.
