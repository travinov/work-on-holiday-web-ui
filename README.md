# Work on Holiday Web UI

Приложение для оформления заявок на работу в выходной день, фиксации фактически отработанного времени и формирования Excel-отчетов для администратора.

Текущая версия работает без загрузки Excel/CSV-выгрузок из опроса. Источник данных — зарегистрированные пользователи и заявки, созданные через Web UI в локальной SQLite БД.

## Основной процесс

1. Администратор инициализирует БД.
2. Сотрудник входит по ФИО. При первом входе получает персональный токен.
3. Сотрудник создает одну или несколько заявок на выход в выходной.
4. Администратор формирует общий Excel за выбранную неделю.
5. После работы сотрудник указывает фактически отработанное время по конкретной заявке; датой факта является дата заявки.
6. Администратор формирует отчетность по факту и при необходимости закрывает период.

Подробные ролевые workflow и corner cases описаны в [docs/workflows/employee-admin-workflows.md](docs/workflows/employee-admin-workflows.md).

## Структура БД

### `app_employee_directory`

Зарегистрированные сотрудники Web-only сценария. В актуальном процессе используются ФИО и служебные идентификаторы. Телефоны, должность и числовой грейд не собираются и не проверяются.

- `full_name_key` — нормализованный ключ ФИО;
- `full_name` — ФИО;
- legacy-поля `work_email`, `local_phone`, `mobile_phone`, `position_short_name`, `grade_num` сохраняются только для совместимости схемы и не используются Web-регистрацией;
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
- `app_request_state` — статус заявки, корректировки, факт и группа частей ночной заявки;
- `app_report_lock` — legacy-фиксация старых заявок;
- `app_period_lock` — закрытие приема заявок или ввода факта по неделе.

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

### Рекомендуемая схема: ZIP локально -> deploy на сервер

Схема развертывания:

1. Скачать ZIP из GitHub на локальный корпоративный компьютер.
2. Разархивировать ZIP в локальную рабочую папку проекта с заменой файлов.
3. Открыть терминал в локальной папке проекта.
4. Запустить один из двух deploy-скриптов:
   - `install-to-corporate-server.sh` — только для первичной установки;
   - `update-corporate-server.sh` — для повторных обновлений с сохранением данных.
5. Скрипт сам скопирует проект на сервер по SSH/rsync и запустит no-sudo deploy на сервере.

На локальном компьютере должны быть доступны команды `ssh` и `rsync`, а SSH-доступ должен работать:

```bash
ssh CI09479675-lnx-travinov@tsles-assai0001.esrt.sber.ru
```

ZIP:

```text
https://codeload.github.com/travinov/work-on-holiday-web-ui/zip/refs/heads/main
```

#### Первичная установка

Использовать только один раз, когда на сервере еще нет SQLite-файла БД и env-файла приложения.

```bash
deploy/scripts/install-to-corporate-server.sh
```

Скрипт покажет логин суперпользователя и попросит ввести секрет скрытым вводом в терминале.

Скрипт защищает от случайной перезаписи: если на сервере уже есть БД или env-файл, он остановится и предложит использовать скрипт обновления.

#### Повторное обновление с сохранением данных

Использовать для всех последующих релизов.

```bash
deploy/scripts/update-corporate-server.sh
```

Перед копированием новой версии и применением возможных изменений схемы БД скрипт создает SQL-dump текущего SQLite-файла БД на сервере:

```text
$HOME/.local/state/work-on-holiday/backups/survey_results-pre-update-YYYYMMDD-HHMMSS.sql
```

После dump скрипт:

- копирует новую версию проекта в `~/apps/work-on-holiday`;
- не копирует локальные runtime-данные;
- запускает серверный no-sudo deploy;
- применяет инициализацию/обновление схемы без удаления данных;
- останавливает старый `systemd --user` сервис, если он был;
- запускает приложение в `screen`-сессии `work-on-holiday`;
- устанавливает `crontab` watchdog для автозапуска после reboot и перезапуска при падении.

Скрипты используют настройки:

- сервер: `tsles-assai0001.esrt.sber.ru`;
- SSH-пользователь: `CI09479675-lnx-travinov`;
- удаленная папка проекта: `~/apps/work-on-holiday`;
- bind host: `0.0.0.0`;
- порт приложения: `8081`;
- service name: `work-on-holiday`;
- режим на сервере: no-sudo `screen` + `crontab` watchdog.

Скрипты не копируют локальные runtime-данные:

- `survey_results.db`;
- `reports/`;
- `generated_exports/`;
- `restore_points/`;
- `backups/`;
- `venv/`.

На сервере SQLite-файл БД хранится вне папки проекта:

- БД: `$HOME/.local/share/work-on-holiday/survey_results.db`;
- backup и SQL-dump: `$HOME/.local/state/work-on-holiday/backups`;
- env-файл: `$HOME/.config/work-on-holiday/work-on-holiday.env`;
- start-скрипт: `$HOME/.local/bin/work-on-holiday-start.sh`;
- watchdog-скрипт: `$HOME/.local/bin/work-on-holiday-watchdog.sh`;
- лог приложения: `$HOME/.local/state/work-on-holiday/logs/work-on-holiday.log`;
- crontab: `@reboot` запуск и ежеминутная health-check проверка.

Проверка с локального компьютера через SSH:

```bash
ssh CI09479675-lnx-travinov@tsles-assai0001.esrt.sber.ru \
  'curl -I http://127.0.0.1:8081/ && screen -ls | grep work-on-holiday && crontab -l | grep work-on-holiday'
```

Проверка внешнего доступа с локального компьютера:

```bash
curl -I http://tsles-assai0001.esrt.sber.ru:8081/
```


### Несколько проектов на одном сервере

Если рядом разворачивается другой проект, например `RoleModel_helper`, нужно развести:

- порт приложения;
- имя systemd-сервиса;
- папку приложения;
- путь к SQLite-файлу БД;
- папку backup;
- env-файл.

Для Work on Holiday зарезервированы:

- service: `work-on-holiday`;
- port: `8081`;
- системный SQLite-файл БД: `/var/lib/work-on-holiday/survey_results.db`;
- пользовательский SQLite-файл БД: `$HOME/.local/share/work-on-holiday/survey_results.db`.

Для `RoleModel_helper` нужно использовать другие значения, например другой service name и другой порт, чтобы приложения не перезаписывали unit/env/SQLite-файлы БД друг друга.

### Альтернатива: развертывание с локальной машины на корпоративный сервер

С локальной машины проект отправляется на сервер через SSH/rsync, а затем на сервере запускается безопасный deploy с backup SQLite-файла БД:

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

SQLite-файл БД остается на сервере. При повторном deploy перед обновлением схемы создается backup SQLite-файла БД.

### Базовый серверный скрипт

Если проект уже находится на сервере, основной скрипт:

```bash
sudo WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-server' deploy/scripts/deploy-server.sh
```

Что делает скрипт:

- создает `venv`, если его еще нет;
- устанавливает зависимости из `requirements.txt`;
- при первом развертывании создает `survey_results.db`;
- при повторном развертывании сначала делает backup существующего SQLite-файла БД в `backups/`;
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
  --date-to 2026-04-26
```

Результат:

- файл `Отчеты выхода выходные <период>.xlsx`;
- лист `Отчет 1` — отчет для руководства по плановым заявкам;
- лист `Отчет 2` — краткий отчет для заведения заявок;
- лист `Отчет 3` — фактически отработанное время;
- лист `Отчет 4` — сверка заявок и факта.

CSV сотрудников для формирования отчетов не нужен. Признак `Грейд 12+` читается из профиля зарегистрированного пользователя. Проверка мобильного телефона не выполняется.

Если плановый интервал переходит через полночь, Web UI заранее показывает сообщение `Будет создано две заявки`, а после обычного сохранения атомарно создает две связанные заявки по календарным датам. Для каждой части отдельно при длительности от пяти часов выводится предупреждение `Из рабочего времени будет вычтен 1 час на обед`; система не меняет и не уменьшает введенное время.

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
