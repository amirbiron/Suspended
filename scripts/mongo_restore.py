#!/usr/bin/env python3
import os
import sys
import pathlib

from pymongo import MongoClient
from bson.json_util import loads


def main() -> int:
	if len(sys.argv) < 2:
		print("Usage: mongo_restore.py <input_dir>")
		return 2

	input_dir = pathlib.Path(sys.argv[1])
	if not input_dir.exists() or not input_dir.is_dir():
		print(f"Input dir not found: {input_dir}")
		return 1

	mongo_uri = os.environ.get("MONGODB_URI")
	if not mongo_uri:
		print("MONGODB_URI not set; cannot restore MongoDB.")
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
		# fallback: take db from URI path if present
		path = mongo_uri.split("/", 3)[-1]
		db_name = path.split("?", 1)[0] or "admin"

	db = client[db_name]

	for file in input_dir.glob("*.jsonl"):
		coll_name = file.stem
		coll = db[coll_name]
		# strategy: replace collection contents with backup
		coll.delete_many({})
		insert_batch = []
		with file.open("r", encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				insert_batch.append(loads(line))
		if insert_batch:
			coll.insert_many(insert_batch)
		print(f"Restored {len(insert_batch)} docs -> {coll_name}")

	print("✅ Mongo JSON restore complete")
	return 0


if __name__ == "__main__":
	sys.exit(main())