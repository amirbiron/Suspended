#!/usr/bin/env bash
set -euo pipefail

# Configurable
BACKUP_ROOT=${BACKUP_ROOT:-"/workspace/_backups"}
PROJECT_DIR=${PROJECT_DIR:-"/workspace"}
ENV_FILE=${ENV_FILE:-"${PROJECT_DIR}/.env"}
MONGO_URI=${MONGODB_URI:-""}
TELEGRAM_TOKEN=${TELEGRAM_BOT_TOKEN:-""}

TS=$(date +%Y%m%d_%H%M%S)
DEST_DIR="${BACKUP_ROOT}/${TS}"
mkdir -p "${DEST_DIR}"

# 1) Save git state
if command -v git >/dev/null 2>&1 && [ -d "${PROJECT_DIR}/.git" ]; then
	GIT_HASH=$(git -C "${PROJECT_DIR}" rev-parse --short HEAD || echo "nogit")
	git -C "${PROJECT_DIR}" status --porcelain > "${DEST_DIR}/git_status.txt" || true
	echo "${GIT_HASH}" > "${DEST_DIR}/git_hash.txt"
else
	echo "nogit" > "${DEST_DIR}/git_hash.txt"
fi

# 2) Archive project code (excluding heavy/irrelevant dirs)
TAR_EXCLUDES=(
	--exclude=.git
	--exclude=node_modules
	--exclude=venv
	--exclude=__pycache__
	--exclude=_backups
)

tar -czf "${DEST_DIR}/project.tar.gz" -C "${PROJECT_DIR}" "${TAR_EXCLUDES[@]}" .

# 3) Copy .env if exists
if [ -f "${ENV_FILE}" ]; then
	cp "${ENV_FILE}" "${DEST_DIR}/env.backup"
fi

# 4) MongoDB dump
if [ -n "${MONGO_URI}" ]; then
	if command -v mongodump >/dev/null 2>&1; then
		mongodump --uri="${MONGO_URI}" --archive="${DEST_DIR}/mongo.archive" --gzip || true
		echo "mongo_mode=archive" > "${DEST_DIR}/mongo.mode"
	else
		python3 "${PROJECT_DIR}/scripts/mongo_backup.py" "${DEST_DIR}/mongo_json" || true
		echo "mongo_mode=json" > "${DEST_DIR}/mongo.mode"
	fi
else
	echo "Skipping Mongo dump (missing MONGODB_URI)." > "${DEST_DIR}/mongo.SKIPPED.txt"
fi

# 5) Telegram commands export (default scope)
if [ -n "${TELEGRAM_TOKEN}" ]; then
	curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMyCommands" -o "${DEST_DIR}/telegram_commands_default.json" || true
	curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMyCommands" -H "Content-Type: application/json" -d '{"scope":{"type":"all_private_chats"}}' -o "${DEST_DIR}/telegram_commands_private.json" || true
	curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMyCommands" -H "Content-Type: application/json" -d '{"scope":{"type":"all_group_chats"}}' -o "${DEST_DIR}/telegram_commands_groups.json" || true
	curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMyCommands" -H "Content-Type: application/json" -d '{"scope":{"type":"all_chat_administrators"}}' -o "${DEST_DIR}/telegram_commands_admins.json" || true
else
	echo "Skipping Telegram commands export (missing TELEGRAM_BOT_TOKEN)." > "${DEST_DIR}/telegram.SKIPPED.txt"
fi

# 6) Summary
cat > "${DEST_DIR}/README.txt" <<EOF
Backup created at ${TS}
- Git hash: $(cat "${DEST_DIR}/git_hash.txt")
- Project tar: project.tar.gz
- .env: env.backup (if existed)
- Mongo: if mongo.mode=archive -> mongo.archive (gzip); if mongo.mode=json -> mongo_json/*.jsonl
- Telegram commands: telegram_commands_*.json if token provided

To restore, see restore.sh
EOF

echo "✅ Backup created at: ${DEST_DIR}"