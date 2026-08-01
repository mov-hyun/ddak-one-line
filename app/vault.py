from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


DEMO_CONTACTS = [
    {
        "contact_ref": "contact:self",
        "relationship": "본인",
        "aliases": ["나", "내", "본인", "홍길동"],
        "name_ko": "홍길동",
        "name_en": "HONG GIL DONG",
        "address_domestic": "서울 서초구 사평대로57길 44 2층, 올댓마인드 신논현점",
        "postal_code": "06541",
        "address_base": "서울특별시 서초구 사평대로57길 44 (반포동)",
        "address_detail": "2층, 올댓마인드 신논현점",
        "address_base_en": "44, Sapyeong-daero 57-gil, Seocho-gu, Seoul",
        "address_detail_en": "2F, Allthatmind Sinnonhyeon",
        "address_international": "",
        "phone": "010-1111-1111",
        "email": "",
        "guest_password": "1111",
    },
    {
        "contact_ref": "contact:eldest_daughter",
        "relationship": "큰딸",
        "aliases": ["큰딸", "첫째딸", "임꺽정"],
        "name_ko": "임꺽정",
        "name_en": "",
        "address_domestic": "서울 종로구 사직로 161 경복궁",
        "postal_code": "03045",
        "address_base": "서울특별시 종로구 사직로 161 (세종로, 경복궁)",
        "address_detail": "경복궁",
        "address_international": "",
        "phone": "010-2222-2222",
        "email": "",
    },
    {
        "contact_ref": "contact:younger_daughter",
        "relationship": "작은딸",
        "aliases": ["작은딸", "둘째딸", "장보고"],
        "name_ko": "장보고",
        "name_en": "JANG BO GO",
        "address_domestic": "",
        "address_international": "200 Santa Monica Pier, Santa Monica, CA 90401 USA",
        "phone": "+1-10-3333-3333",
        "email": "",
    },
]


@dataclass
class ContactMatch:
    contact_ref: str
    relationship: str


class VaultRepository:
    def __init__(self, db_path: Path, key_path: Path) -> None:
        self.db_path = db_path
        self.key_path = key_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())
        self._initialize()

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        return key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    contact_ref TEXT PRIMARY KEY,
                    relationship TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL
                )
                """
            )
            for contact in DEMO_CONTACTS:
                self.upsert_contact(contact, connection=connection)

    def upsert_contact(
        self,
        contact: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        payload = {
            key: value
            for key, value in contact.items()
            if key not in {"contact_ref", "relationship", "aliases"}
        }
        encrypted = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            connection.execute(
                """
                INSERT INTO contacts(contact_ref, relationship, aliases_json, encrypted_payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_ref) DO UPDATE SET
                    relationship=excluded.relationship,
                    aliases_json=excluded.aliases_json,
                    encrypted_payload=excluded.encrypted_payload
                """,
                (
                    contact["contact_ref"],
                    contact["relationship"],
                    json.dumps(contact.get("aliases", []), ensure_ascii=False),
                    encrypted,
                ),
            )
            if owns_connection:
                connection.commit()
        finally:
            if owns_connection:
                connection.close()

    def resolve_relationship(self, relationship: str) -> ContactMatch | None:
        normalized = relationship.replace(" ", "").lower()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT contact_ref, relationship, aliases_json FROM contacts"
            ).fetchall()
        for row in rows:
            aliases = json.loads(row["aliases_json"])
            candidates = [row["relationship"], *aliases]
            if any(normalized == str(value).replace(" ", "").lower() for value in candidates):
                return ContactMatch(row["contact_ref"], row["relationship"])
        return None

    def get_contact(self, contact_ref: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT contact_ref, relationship, encrypted_payload FROM contacts WHERE contact_ref = ?",
                (contact_ref,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(self._fernet.decrypt(row["encrypted_payload"]).decode("utf-8"))
        return {
            "contact_ref": row["contact_ref"],
            "relationship": row["relationship"],
            **payload,
        }

    def health(self) -> dict[str, Any]:
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        return {"ready": count >= 3, "contact_count": count, "encrypted": True}
