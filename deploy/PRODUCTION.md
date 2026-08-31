# Work on Holiday: Production на том же сервере

> Важно: эти скрипты запускаются только из отдельно проверенной Production-сборки.
> Сборку тестового контура устанавливать в Production запрещено.

Production устанавливается рядом с текущим экземпляром и не использует его
runtime-файлы.

| Ресурс | Текущий экземпляр | Production |
|---|---|---|
| Порт | `8081` | `8082` |
| Код | `~/apps/work-on-holiday` | `~/apps/work-on-holiday-production` |
| Имя экземпляра | `work-on-holiday` | `work-on-holiday-production` |
| SQLite | `~/.local/share/work-on-holiday/survey_results.db` | `~/.local/share/work-on-holiday-production/survey_results.db` |
| Backup | `~/.local/state/work-on-holiday/backups` | `~/.local/state/work-on-holiday-production/backups` |
| Env | `~/.config/work-on-holiday/work-on-holiday.env` | `~/.config/work-on-holiday-production/work-on-holiday-production.env` |
| Screen | `work-on-holiday` | `work-on-holiday-production` |
| Лог | `~/.local/state/work-on-holiday/logs/work-on-holiday.log` | `~/.local/state/work-on-holiday-production/logs/work-on-holiday-production.log` |

## Первая установка

1. Скачать ZIP проекта на корпоративный компьютер.
2. Распаковать ZIP в отдельную локальную папку.
3. Открыть Terminal в корне распакованного проекта.
4. Запустить:

```bash
deploy/scripts/install-production-to-corporate-server.sh
```

Скрипт запросит отдельный пароль суперпользователя Production скрытым вводом.
До копирования файлов он проверит, что Production-БД и env-файл еще не
существуют, а порт `8082` свободен. Затем создаст новую пустую Production-БД.
Текущая БД на порту `8081` не копируется и не изменяется.

## Обновление Production

Для каждого следующего релиза из новой распакованной ZIP-копии запускать:

```bash
deploy/scripts/update-production-corporate-server.sh
```

Перед обновлением создается SQL-dump Production-БД в
`~/.local/state/work-on-holiday-production/backups`. Данные текущего экземпляра
не затрагиваются.

## Проверка

Проверка обоих экземпляров с корпоративного компьютера:

```bash
ssh CI09479675-lnx-travinov@tsles-assai0001.esrt.sber.ru \
  "curl -fsS -o /dev/null -w 'current 8081: HTTP %{http_code}\n' http://127.0.0.1:8081/ \
  && curl -fsS -o /dev/null -w 'production 8082: HTTP %{http_code}\n' http://127.0.0.1:8082/ \
  && screen -ls | grep 'work-on-holiday' \
  && crontab -l | grep 'work-on-holiday'"
```

Внешний адрес Production:

```text
http://tsles-assai0001.esrt.sber.ru:8082/
```

Успешная установка должна дать `HTTP 200`, отдельную screen-сессию
`work-on-holiday-production` и две отдельные пары записей watchdog в crontab.
