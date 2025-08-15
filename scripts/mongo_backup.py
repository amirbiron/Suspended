#!/usr/bin/env python3
import os
import sys
import pathlib
from typing import Optional

from pymongo import MongoClient
from bson.json_util import dumps


def get_database_name_from_uri(uri: str) -> Optional[str]:
	try:
		# mongodb+srv://user:pass@host/dbname?options
		path = uri.split("/", 3)[-1]
		dbname = path.split("?", 1)[0]
		return dbname if dbname else None
	except Exception:
		return None


def main() -> int:
	if len(sys.argv) < 2:
		print("Usage: mongo_backup.py <output_dir>")
		return 2

	output_dir = pathlib.Path(sys.argv[1])
	output_dir.mkdir(parents=True, exist_ok=True)

	mongo_uri = os.environ.get("MONGODB_URI")
	if not mongo_uri:
		print("MONGODB_URI not set; cannot back up MongoDB.")
		return 0

	client = MongoClient(mongo_uri)

	# Resolve database name
	db_name = None
	try:
		import config  # type: ignore
		db_name = getattr(config, "DATABASE_NAME", None)
	except Exception:
		pass

	if not db_name:
		db_name = get_database_name_from_uri(mongo_uri) or "admin"

	db = client[db_name]

	collections = db.list_collection_names()
	for coll_name in collections:
		coll = db[coll_name]
		out_file = output_dir / f"{coll_name}.jsonl"
		with out_file.open("w", encoding="utf-8") as f:
			for doc in coll.find({}):
				f.write(dumps(doc))
				f.write("\n")
		print(f"Exported collection '{coll_name}' -> {out_file}")

	print(f"✅ Mongo JSON backup written to {output_dir}")
	return 0


if __name__ == "__main__":
	sys.exit(main())