from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


TEXT_FIELDS = ("nama", "jab", "jt", "jenjang", "inst", "hasil", "batch", "mulai", "sel", "chk")


class DatasetValidationError(ValueError):
    pass


def validate_dataset(
    rows: list[dict[str, Any]], soal_count: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    validated_rows: list[dict[str, Any]] = []
    participants: dict[str, str] = {}

    for index, original in enumerate(rows, start=1):
        nip = str(original.get("nip") or "").strip()
        name = str(original.get("nama") or "").strip()
        if len(nip) > 64 or len(name) > 200:
            raise DatasetValidationError(f"Baris {index}: NIP atau nama terlalu panjang.")

        row = dict(original)
        row["nip"] = nip
        row["nama"] = name
        for field in TEXT_FIELDS:
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                raise DatasetValidationError(
                    f"Baris {index}: format kolom {field} tidak valid."
                )
            if isinstance(value, str) and len(value) > 1000:
                raise DatasetValidationError(f"Baris {index}: kolom {field} terlalu panjang.")

        scores = row.get("s")
        if not isinstance(scores, list) or len(scores) != soal_count:
            raise DatasetValidationError(
                f"Baris {index}: jumlah nilai unit kompetensi tidak valid."
            )
        for score in scores:
            if score is not None and (
                not isinstance(score, (int, float))
                or not math.isfinite(score)
                or score < 0
                or score > 5
            ):
                raise DatasetValidationError(
                    f"Baris {index}: nilai unit kompetensi tidak valid."
                )

        if nip and name:
            previous_name = participants.get(nip)
            if previous_name is not None and previous_name.casefold() != name.casefold():
                raise DatasetValidationError(
                    "Satu NIP tercatat dengan beberapa nama. Perbaiki data Excel sebelum memuatnya."
                )
            participants[nip] = name
        validated_rows.append(row)

    return validated_rows, participants


def persist_dataset(
    connection: sqlite3.Connection,
    filename: str,
    soal_count: int,
    rows: list[dict[str, Any]],
    participants: dict[str, str],
) -> None:
    previous = {
        row["nip"]: row
        for row in connection.execute(
            "SELECT id, nip, password_hash, must_change_password FROM accounts WHERE role = 'user'"
        ).fetchall()
    }
    for nip in set(previous) - set(participants):
        connection.execute("DELETE FROM accounts WHERE role = 'user' AND nip = ?", (nip,))

    connection.execute("DELETE FROM dataset_rows")
    connection.executemany(
        "INSERT INTO dataset_rows (nip, payload) VALUES (?, ?)",
        (
            (row["nip"], json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            for row in rows
        ),
    )

    for nip, name in participants.items():
        old = previous.get(nip)
        password_hash = old["password_hash"] if old else ""
        must_change = old["must_change_password"] if old else 1
        connection.execute(
            """
            INSERT INTO accounts (username, username_key, role, nip, password_hash, must_change_password)
            VALUES (?, ?, 'user', ?, ?, ?)
            ON CONFLICT(nip) DO UPDATE SET
                username = excluded.username,
                username_key = excluded.username_key
            """,
            (name, name.casefold(), nip, password_hash, must_change),
        )

    connection.execute(
        """
        INSERT INTO metadata (key, value) VALUES ('file', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (filename,),
    )
    connection.execute(
        """
        INSERT INTO metadata (key, value) VALUES ('soalCount', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(soal_count),),
    )
    connection.execute(
        "DELETE FROM login_attempts WHERE attempted_at < ?",
        (int(time.time()) - 15 * 60,),
    )


@contextmanager
def connect_db(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
