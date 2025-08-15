#!/usr/bin/env bash
set -euo pipefail

BACKUP_PATH=${1:-}
PROJECT_DIR=${PROJECT_DIR:-"/workspace"}
ENV_FILE=${ENV_FILE:-"${PROJECT_DIR}/.env"}
MONGO_URI=${MONGODB_URI:-""}
TELEGRAM_TOKEN=${TELEGRAM_BOT_TOKEN:-""}

if [ -z "${BACKUP_PATH}" ]; then
	echo "Usage: $0 /workspace/_backups/<TIMESTAMP>"
	exit 1
fi

if [ ! -d "${BACKUP_PATH}" ]; then
	echo "Backup directory not found: ${BACKUP_PATH}"
	exit 1
fi

# 1) Restore project code
if [ -f "${BACKUP_PATH}/project.tar.gz" ]; then
	tar -xzf "${BACKUP_PATH}/project.tar.gz" -C "${PROJECT_DIR}"
	echo "Restored project files to ${PROJECT_DIR}"
else
	echo "Project archive not found, skipping."
fi

# 2) Restore .env
if [ -f "${BACKUP_PATH}/env.backup" ]; then
	cp "${BACKUP_PATH}/env.backup" "${ENV_FILE}"
	echo "Restored .env to ${ENV_FILE}"
fi

# 3) Restore MongoDB
if [ -n "${MONGO_URI}" ] && command -v mongorestore >/dev/null 2>&1 && [ -f "${BACKUP_PATH}/mongo.archive" ]; then
	mongorestore --uri="${MONGO_URI}" --archive="${BACKUP_PATH}/mongo.archive" --gzip || true
	echo "Restored MongoDB from archive"
else
	echo "Skipping Mongo restore (missing MONGODB_URI/mongorestore or archive missing)."
fi

# 4) Restore Telegram commands (default scope only by default)
if [ -n "${TELEGRAM_TOKEN}" ]; then
	if [ -f "${BACKUP_PATH}/telegram_commands_default.json" ]; then
		CMDS=$(jq -c '.result // []' "${BACKUP_PATH}/telegram_commands_default.json" 2>/dev/null || echo '[]')
		curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setMyCommands" \
			-H "Content-Type: application/json" \
			-d "{\"commands\": ${CMDS}, \"scope\": {\"type\": \"default\"}}" >/dev/null || true
		echo "Restored Telegram default commands"
	fi
	# Optional: restore other scopes if needed
	for scope in private groups admins; do
		FILE="${BACKUP_PATH}/telegram_commands_${scope}.json"
		[ -f "$FILE" ] || continue
		CMDS=$(jq -c '.result // []' "$FILE" 2>/dev/null || echo '[]')
		SCOPE_TYPE=$(case $scope in private) echo all_private_chats;; groups) echo all_group_chats;; admins) echo all_chat_administrators;; esac)
		curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setMyCommands" \
			-H "Content-Type: application/json" \
			-d "{\"commands\": ${CMDS}, \"scope\": {\"type\": \"${SCOPE_TYPE}\"}}" >/dev/null || true
		echo "Restored Telegram ${scope} commands"
	done
else
	echo "Skipping Telegram commands restore (missing TELEGRAM_BOT_TOKEN)."
fi

echo "✅ Restore complete from: ${BACKUP_PATH}"