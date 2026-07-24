from __future__ import annotations

import argparse
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from src.app_request_state import ensure_app_tables
    from src.db_schema import ensure_core_tables
except ModuleNotFoundError:
    from app_request_state import ensure_app_tables
    from db_schema import ensure_core_tables


LAST_NAMES = [
    "Иванов",
    "Петров",
    "Сидоров",
    "Смирнов",
    "Кузнецов",
    "Васильев",
    "Орлов",
    "Федоров",
    "Морозов",
    "Волков",
    "Соколов",
    "Попов",
]
FIRST_NAMES = [
    "Алексей",
    "Дмитрий",
    "Сергей",
    "Михаил",
    "Николай",
    "Павел",
    "Илья",
    "Роман",
    "Денис",
    "Виктор",
    "Андрей",
    "Кирилл",
]
PATRONYMICS = [
    "Иванович",
    "Петрович",
    "Сергеевич",
    "Алексеевич",
    "Дмитриевич",
    "Николаевич",
    "Викторович",
    "Олегович",
]
POSITIONS = [
    "инженер сопровождения",
    "системный аналитик",
    "разработчик",
    "ведущий инженер",
    "эксперт сопровождения",
]


@dataclass(frozen=True)
class GeneratedUser:
    full_name_key: str
    full_name: str
    work_email: str
    local_phone: str
    mobile_phone: str
    position_short_name: str
    grade_num: int
    grade_12_plus: bool


def normalize_name_key(value: str) -> str:
    return " ".join(value.strip().split()).lower().replace("ё", "е")


def build_user(index: int, rng: random.Random) -> GeneratedUser:
    last_name = LAST_NAMES[index % len(LAST_NAMES)]
    first_name = FIRST_NAMES[(index + rng.randint(0, len(FIRST_NAMES) - 1)) % len(FIRST_NAMES)]
    patronymic = PATRONYMICS[(index + rng.randint(0, len(PATRONYMICS) - 1)) % len(PATRONYMICS)]
    full_name = f"{last_name} {first_name} {patronymic}"
    grade_num = 6 + (index % 8)
    mobile_suffix = 1000000 + index
    return GeneratedUser(
        full_name_key=normalize_name_key(full_name),
        full_name=full_name,
        work_email=f"user{index + 1:03d}@example.local",
        local_phone=f"8-{88000000 + index:08d}",
        mobile_phone=f"7916{mobile_suffix:07d}",
        position_short_name=POSITIONS[index % len(POSITIONS)],
        grade_num=grade_num,
        grade_12_plus=grade_num >= 12,
    )


def generate_users(db_path: str | Path, *, count: int = 10, seed: int = 42, overwrite: bool = False) -> list[GeneratedUser]:
    if count < 1:
        raise ValueError("count должен быть больше 0")

    rng = random.Random(seed)
    users = [build_user(index, rng) for index in range(count)]
    now = datetime.now().isoformat(timespec="seconds")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(path)) as conn, conn:
        ensure_core_tables(conn)
        ensure_app_tables(conn)
        if overwrite:
            conn.execute("DELETE FROM app_employee_auth;")
            conn.execute("DELETE FROM app_employee_profile;")
            conn.execute("DELETE FROM app_employee_directory;")
        for user in users:
            conn.execute(
                """
                INSERT INTO app_employee_directory (
                    full_name_key,
                    full_name,
                    work_email,
                    local_phone,
                    mobile_phone,
                    position_short_name,
                    grade_num,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name_key) DO UPDATE SET
                    full_name = excluded.full_name,
                    work_email = excluded.work_email,
                    local_phone = excluded.local_phone,
                    mobile_phone = excluded.mobile_phone,
                    position_short_name = excluded.position_short_name,
                    grade_num = excluded.grade_num,
                    updated_at = excluded.updated_at;
                """,
                (
                    user.full_name_key,
                    user.full_name,
                    user.work_email,
                    user.local_phone,
                    user.mobile_phone,
                    user.position_short_name,
                    user.grade_num,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO app_employee_profile (full_name_key, grade_12_plus, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(full_name_key) DO UPDATE SET
                    grade_12_plus = excluded.grade_12_plus,
                    updated_at = excluded.updated_at;
                """,
                (user.full_name_key, 1 if user.grade_12_plus else 0, now),
            )
        conn.commit()
    return users


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Генерация тестовых пользователей Work on Holiday")
    parser.add_argument("--db", default="survey_results.db", help="Путь к SQLite БД")
    parser.add_argument("--count", type=int, default=10, help="Количество пользователей")
    parser.add_argument("--seed", type=int, default=42, help="Seed для повторяемой генерации")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Очистить справочник пользователей, профили и токены перед генерацией",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    users = generate_users(args.db, count=args.count, seed=args.seed, overwrite=args.overwrite)
    print(f"Создано/обновлено пользователей: {len(users)}")
    for user in users:
        grade_label = "12+" if user.grade_12_plus else str(user.grade_num)
        print(f"- {user.full_name}; grade={grade_label}; email={user.work_email}")


if __name__ == "__main__":
    main()
