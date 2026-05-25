# Work on Holiday: серверный HTTPS с self-signed сертификатом

Этот вариант оставляет FastAPI на `127.0.0.1:8080` и ставит перед ним `nginx` на `443`.
Локальный HTTP-режим разработки не меняется.

## Что получится

```text
https://SERVER_IP
  -> nginx:443, self-signed TLS
  -> http://127.0.0.1:8080
  -> FastAPI
```

Браузер будет показывать предупреждение безопасности, пока сертификат не добавлен в доверенные на рабочих местах.

## 1. Скопировать проект на сервер

Рекомендуемый путь в примерах:

```bash
/opt/work-on-holiday
```

Создать системного пользователя:

```bash
sudo useradd --system --home /opt/work-on-holiday --shell /usr/sbin/nologin workholiday
sudo chown -R workholiday:workholiday /opt/work-on-holiday
```

Установить зависимости проекта в `/opt/work-on-holiday/venv`.

## 2. Сгенерировать self-signed сертификат

Для доступа по IP:

```bash
cd /opt/work-on-holiday
sudo deploy/scripts/generate-self-signed-cert.sh SERVER_IP SERVER_IP /etc/ssl/work-on-holiday
```

Пример:

```bash
sudo deploy/scripts/generate-self-signed-cert.sh 10.10.10.25 10.10.10.25 /etc/ssl/work-on-holiday
```

Для внутреннего имени и IP:

```bash
sudo deploy/scripts/generate-self-signed-cert.sh work-holiday.internal 10.10.10.25 /etc/ssl/work-on-holiday
```

## 3. Установить systemd service

Перед установкой заменить пароль суперпользователя в `deploy/systemd/work-on-holiday.service`:

```ini
Environment=WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=change-me-on-server
```

Установить сервис:

```bash
sudo cp deploy/systemd/work-on-holiday.service /etc/systemd/system/work-on-holiday.service
sudo systemctl daemon-reload
sudo systemctl enable --now work-on-holiday.service
sudo systemctl status work-on-holiday.service
```

Проверить внутренний HTTP:

```bash
curl -sS http://127.0.0.1:8080/ -o /tmp/work-on-holiday.html -w 'http:%{http_code}\n'
```

## 4. Установить nginx HTTPS proxy

```bash
sudo cp deploy/nginx/work-on-holiday-self-signed.conf /etc/nginx/sites-available/work-on-holiday.conf
sudo ln -s /etc/nginx/sites-available/work-on-holiday.conf /etc/nginx/sites-enabled/work-on-holiday.conf
sudo nginx -t
sudo systemctl reload nginx
```

Если используется RHEL/CentOS-подобный сервер, путь может быть `/etc/nginx/conf.d/work-on-holiday.conf`.

## 5. Проверка HTTPS

Проверка с игнорированием self-signed предупреждения:

```bash
curl -k https://SERVER_IP/ -o /tmp/work-on-holiday-https.html -w 'https:%{http_code}\n'
```

В браузере открыть:

```text
https://SERVER_IP/
```

Предупреждение браузера ожидаемо для self-signed сертификата.

## 6. Важные параметры приложения

Для серверного HTTPS режима сервис включает:

```bash
WORK_ON_HOLIDAY_SECURE_COOKIES=1
```

Это делает cookie доступными только через HTTPS.
Для локального запуска без HTTPS этот параметр не задавать.

## 7. Откат

Отключить nginx-конфиг:

```bash
sudo rm -f /etc/nginx/sites-enabled/work-on-holiday.conf
sudo nginx -t
sudo systemctl reload nginx
```

Остановить приложение:

```bash
sudo systemctl disable --now work-on-holiday.service
```
