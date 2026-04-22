# Prompt For QwenCLI: Корпоративный Бот По Выходам В Выходные

## 1) Финальный мастер-промпт (вставить в QwenCLI)

```text
You are a senior Python engineer. Build a production-ready corporate messenger bot in 5 strict iterations with acceptance gates. Do NOT skip gates. Do not jump ahead.

Stack:
- Python 3.11+
- sber-sberchat-bot-sdk==4.15.0
- sber-sberchat-api-schema==1.318.0

Mandatory dependency policy:
- Use exact pinned versions only:
  - sber-sberchat-bot-sdk==4.15.0
  - sber-sberchat-api-schema==1.318.0
- Put these exact pins into requirements.txt (no ranges).
- All bot API integration must be compatible with these exact versions.
- If any generic example conflicts, prioritize these versions.

Project goal:
- Build an admin bot for weekend-work reporting.
- Reuse existing project scripts/modules:
  - src/etl_processor.py
  - src/report_first_management.py
  - src/report_second_requests.py
  - src/report_third_closure.py
  - src/build_weekend_reports.py
- Admin can run ETL and receive Excel reports in chat.

Domain/business rules (must preserve exactly):
- Dedup logic: keep latest by start_time, then source_row, then response_id.
- Report 1 and Report 2:
  - only “Подать заявку”
  - only records without matching factual exit on same employee/date.
- Report 3:
  - only “Указать отработанное время”
  - actual date and actual time must be filled.
- Add “Количество выходов за последний месяц” for report 1/2 by factual exits in previous 30 days from planned date.
- Cross-midnight normalization:
  - if factual range crosses midnight (example 23:00-05:00),
  - shift date to next day and convert time to 00:00-duration (00:00-06:00 in example).
- Report 1 AS formatting:
  - split systems by “|”;
  - for each system keep only suffix after last “->”;
  - render each system on a new line in same cell.
- CSV employee validation for report 1:
  - CSV schema:
    EMP_ID,EMP_FULL_NAME,EMP_WORK_EMAIL_TXT,LOCAL_PHONE_TXT,MOBILE_PHONE_TXT,EMP_POSTN_SHORT_NAME,EMP_GRADE_NUM
  - if “Условия выхода” = “Двойная оплата” and EMP_GRADE_NUM >= 12:
    - replace text with “Двойная оплата (грейд 12+)”
    - apply red + bold formatting in Excel cell.
  - if employee is found in CSV and MOBILE_PHONE_TXT is empty:
    - add column “Комментарий” at end of report 1
    - value: “Отсутствует мобильный номер телефона в Пульс.”

Bot requirements:
- Commands:
  - /start
  - /help
  - /admin
- /admin requires authorization (allowlist by corporate user id/email).
- After successful /admin authorization, available actions:
  - run ETL from incoming folder
  - generate merged workbook (all 3 reports)
  - generate report 1 only
  - generate report 2 only
  - generate report 3 only
  - show last ETL/report run status
- Send generated Excel files as chat attachments.

Architecture requirements:
- Clean layers:
  - bot/handlers/
  - bot/services/
  - bot/adapters/
  - bot/config/
  - tests/
- Config via .env + typed settings.
- Structured logging (INFO/WARN/ERROR).
- User-safe error handling: no raw traceback in chat.
- Reuse existing reporting logic via service layer.

Deliverables:
- Full code (no TODO placeholders).
- requirements.txt with exact versions above.
- .env.example.
- Updated README:
  - setup
  - env vars
  - run commands
  - admin command usage
  - ETL/report workflow
- Tests for:
  - /admin auth allow/deny
  - grade warning behavior
  - missing mobile comment behavior
  - workbook generation smoke test

Execution plan: 5 strict iterations with gates

Iteration 1: Skeleton + Config + SDK adapter
- Create project structure and minimal runnable app.
- Add:
  - requirements.txt with pinned SDK/schema versions
  - .env.example
  - typed settings loader
  - adapter interface for sber chat SDK and mock adapter
  - command router skeleton for /start /help /admin
- Gate output:
  - file tree
  - run command
  - short rationale
- Stop and wait for “GO 2”.

Iteration 2: Admin auth + guard
- Implement admin allowlist authorization.
- Protect /admin and admin actions.
- Log denied access attempts.
- Add tests:
  - admin allowed
  - admin denied
- Gate output:
  - changed files only
  - test command
  - sample logs
- Stop and wait for “GO 3”.

Iteration 3: ETL + report integration
- Implement service wrappers for existing scripts/modules.
- Implement admin actions:
  - run ETL
  - generate merged workbook
  - generate report1/report2/report3
  - return last run status
- Send files via SDK attachment API.
- Gate output:
  - integration points
  - admin command matrix
  - manual test steps
- Stop and wait for “GO 4”.

Iteration 4: Validation/formatting parity
- Ensure report 1 applies:
  - grade warning text + red bold
  - missing mobile comment column
  - AS multiline formatting
- Add/extend tests:
  - warning formatting logic
  - missing mobile comment logic
  - workbook smoke test
- Gate output:
  - test evidence
  - before/after sample row
- Stop and wait for “GO 5”.

Iteration 5: Hardening + docs + final
- Improve resilience, logging, and error messages.
- Finalize README and examples.
- Validate end-to-end local run.
- Final gate output:
  - final file tree
  - exact run commands
  - acceptance checklist with pass/fail

Output style rules:
- Be concise and concrete.
- Prioritize code and executable commands.
- At each gate provide only requested artifacts.
- Do not proceed to next iteration without explicit “GO N”.
```

---

## 2) Блок анкеты с ветвлением, значениями и подсказками (добавить в prompt/README бота)

```text
Диалоговая анкета: ветвление, значения, подсказки

Общие правила:
- Q1 не задавать пользователю вручную: ФИО получать из API корпоративного мессенджера по user_id.
- Перед сохранением показывать пользователю сводку и запрашивать подтверждение.
- Все даты хранить в ISO (`YYYY-MM-DD`), в интерфейсе показывать `DD.MM.YYYY`.
- Время хранить строкой формата `HH:MM - HH:MM`.

Ветвление:
- Старт -> Q1 (автозаполнение из API) -> Q2.
- Если Q2 = `Подать заявку`:
  - Q3 -> Q4/Q5 -> Q6 -> Q7 -> блок АС Q8..Q18 -> Q19 -> Q20 -> Q21 -> Подтверждение.
- Если Q2 = `Указать отработанное время`:
  - переход сразу к Q22 -> Q23 -> Подтверждение.
- Для ветки `Указать отработанное время` поля Q3..Q21 не запрашивать.

Вопросы и справочные значения:

Q1. ФИО
- Текст: `Укажите полное ФИО`
- Источник: API мессенджера (`full_name`/эквивалент).
- Подсказка: если API не вернул ФИО, запросить ручной ввод в формате `Фамилия Имя Отчество`.

Q2. Тип обращения
- Текст: `Вы хотите подать заявку или указать отработанное время?`
- Значения: `Подать заявку`, `Указать отработанное время`.
- Подсказка: `Подать заявку` — план работ, `Указать отработанное время` — факт за прошедший выход.

Q3. Грейд 12+
- Текст: `Ваш грейд 12+?`
- Значения: `Да`, `Нет`.
- Подсказка: влияет на условия выхода.

Q4/Q5. Условия выхода
- Логика:
  - если Q3=`Да` -> автоматически установить `Отгул` (вопрос не задавать или показать как read-only).
  - если Q3=`Нет` -> спросить выбор.
- Значения: `Отгул`, `Двойная оплата`.
- Подсказка: выбранное значение попадет в отчеты и валидации.

Q6. Номер/наименование релиза/задачи, описание работ
- Тип: свободный текст.
- Подсказка: можно ссылку Jira/таск + короткое описание (например: `https://.../VV-1234, регламентные работы`).

Q7. Обоснование привлечения
- Тип: свободный текст.
- Подсказка: почему работы нельзя выполнить в рабочее время.

Q8..Q18. Выбор АС
- Q8/Q10/Q12/Q14/Q16/Q18: `Выберите АС`.
- Q9/Q11/Q13/Q15/Q17: `Вам нужно добавить еще одну АС?` (`Да`/`Нет`).
- Ограничение: максимум 6 АС.
- Справочные значения АС (пример):
  - `ЕФС Риск-решения -> ЕФС.Риск-решения.ФП05 Риск ВВВ`
  - `ЕФС Риск-решения -> ЕФС.Риск-решения.ФП02 Launcher`
  - `Пуаро`
  - `Идентификация событий в СМИ`
  - `ППРБ.Риск-экспертиза КИБ -> РЭКИБ.Бизнес планирование`
  - `ППРБ.Риск-экспертиза КИБ -> РЭКИБ.1.Зона проблемности`
- Подсказка: поддержать `Другое` с ручным вводом.

Q19. Плановая дата выхода
- Текст: `Укажите в какой день вы планируете выйти на работы`.
- Формат: `DD.MM.YYYY`.
- Валидация: дата должна быть корректной календарной датой.

Q20. Плановое время работ
- Текст: `Укажите планируемое время работ`.
- Формат: `HH:MM - HH:MM`.
- Подсказка: пример `09:00 - 18:00`.

Q21. Согласующий (Тим-Лид)
- Тип: ФИО или корпоративный логин.
- Подсказка: предпочтительно `Фамилия Имя`.

Q22. Фактическая дата
- Текст: `В какой день вы вышли на работы`.
- Формат: `DD.MM.YYYY`.

Q23. Фактическое время
- Текст: `Время проведения работ`.
- Формат: `HH:MM - HH:MM`.
- Правило: если интервал пересекает полночь (`23:00 - 05:00`), нормализовать при сохранении:
  - дата +1 день,
  - время `00:00 - 06:00`.

Дополнительные требования к UX:
- После каждого ответа показывать короткую подсказку следующего шага.
- В конце анкеты показывать итог в виде карточки:
  - ФИО, тип обращения, условия выхода, задачи, обоснование, АС, даты/время, согласующий.
- Кнопки: `Подтвердить`, `Исправить`.
```

---

## 3) Рекомендуемый порядок использования в QwenCLI

1. Вставить блок из раздела **1**.
2. После генерации каркаса добавить блок из раздела **2** в требования к диалоговому сценарию.
3. Запускать итерации строго командами `GO 2`, `GO 3`, `GO 4`, `GO 5`.
4. На каждом этапе принимать только то, что указано в gate output.

