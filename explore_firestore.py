"""
Esplora Firestore: elenca collezioni e stampa documenti di esempio.

Uso:
  1. Metti la chiave service account in:
       .secrets/firebase-sa.json
     (oppure passa il path con --credentials)

  2. Attiva il venv e installa dipendenze:
       source .venv/bin/activate
       pip install firebase-admin

  3. Elenca le collezioni top-level:
       python explore_firestore.py

  4. Mostra esempi di una collezione:
       python explore_firestore.py --collection NOME_COLLEZIONE

  5. Esporta un dump JSON di una collezione (per analisi, non migrazione finale):
       python explore_firestore.py --collection NOME_COLLEZIONE --export

Opzioni utili:
  --limit N          quanti documenti stampare (default 3)
  --subcollections   elenca anche le sottocollezioni del primo documento
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import DocumentReference, GeoPoint
from google.cloud.firestore_v1._helpers import DatetimeWithNanoseconds

ROOT = Path(__file__).parent
DEFAULT_CREDENTIALS = ROOT / ".secrets" / "firebase-sa.json"
EXPORT_DIR = ROOT / ".secrets" / "firestore_exports"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, DatetimeWithNanoseconds)):
        return value.isoformat()
    if isinstance(value, DocumentReference):
        return {"_ref": value.path}
    if isinstance(value, GeoPoint):
        return {"_geopoint": {"lat": value.latitude, "lng": value.longitude}}
    if isinstance(value, bytes):
        return {"_bytes_b64_len": len(value)}
    return str(value)


def serialize_doc(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data, default=_json_default))


def init_firestore(credentials_path: Path):
    if not credentials_path.exists():
        raise SystemExit(
            f"Chiave non trovata: {credentials_path}\n"
            "Salva il JSON della service account in .secrets/firebase-sa.json"
        )
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(credentials_path))
        firebase_admin.initialize_app(cred)
    return firestore.client()


def list_collections(db) -> list[str]:
    return [c.id for c in db.collections()]


def print_collection_samples(
    db,
    collection_name: str,
    limit: int,
    show_subcollections: bool,
) -> list[dict[str, Any]]:
    col = db.collection(collection_name)
    docs = list(col.limit(limit).stream())
    if not docs:
        print(f"\nCollezione '{collection_name}': vuota (o inesistente).")
        return []

    print(f"\n=== {collection_name} · {len(docs)} documenti di esempio ===")
    exported: list[dict[str, Any]] = []
    for i, doc in enumerate(docs, start=1):
        payload = serialize_doc(doc.to_dict() or {})
        exported.append({"id": doc.id, "data": payload})
        print(f"\n--- documento {i} · id={doc.id} ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        if show_subcollections and i == 1:
            subcols = [s.id for s in doc.reference.collections()]
            print(f"Sottocollezioni di {doc.id}: {subcols or '(nessuna)'}")

    # Conta approssimativa (lettura di tutti gli id: ok per dataset personali)
    total = sum(1 for _ in col.stream())
    print(f"\nTotale documenti in '{collection_name}': {total}")
    return exported


def export_collection(db, collection_name: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc in db.collection(collection_name).stream():
        rows.append({"id": doc.id, "data": serialize_doc(doc.to_dict() or {})})
    out = EXPORT_DIR / f"{collection_name}.json"
    out.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Esplora collezioni e documenti Firestore"
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help="Path al JSON service account",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help="Nome collezione da ispezionare",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Quanti documenti di esempio stampare",
    )
    parser.add_argument(
        "--subcollections",
        action="store_true",
        help="Elenca sottocollezioni del primo documento",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Esporta tutta la collezione in .secrets/firestore_exports/",
    )
    args = parser.parse_args()

    db = init_firestore(args.credentials)

    collections = list_collections(db)
    print("Collezioni top-level:")
    if not collections:
        print("  (nessuna)")
        return
    for name in collections:
        print(f"  - {name}")

    if not args.collection:
        print(
            "\nProssimo passo:\n"
            "  python explore_firestore.py --collection NOME --subcollections\n"
            "Poi, se vuoi un dump completo per analisi:\n"
            "  python explore_firestore.py --collection NOME --export"
        )
        return

    if args.collection not in collections:
        print(
            f"\nAttenzione: '{args.collection}' non risulta tra le collezioni "
            "top-level. Provo comunque a leggerla…"
        )

    print_collection_samples(
        db,
        args.collection,
        limit=args.limit,
        show_subcollections=args.subcollections,
    )

    if args.export:
        path = export_collection(db, args.collection)
        print(f"\nExport salvato in: {path}")


if __name__ == "__main__":
    main()
