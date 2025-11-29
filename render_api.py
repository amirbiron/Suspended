from typing import Any, Dict, List, Optional, cast
from datetime import datetime

import requests

import config


class RenderAPI:
	def __init__(self):
		self.api_key = config.RENDER_API_KEY
		self.base_url = config.RENDER_API_URL
		self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"}

	def suspend_service(self, service_id: str) -> Dict:
		"""השעיית שירות"""
		url = f"{self.base_url}/services/{service_id}/suspend"

		try:
			response = requests.post(url, headers=self.headers, timeout=15)
			return {
				"success": response.status_code == 200,
				"status_code": response.status_code,
				"message": response.text if response.status_code != 200 else "Service suspended successfully",
			}
		except requests.RequestException as e:
			return {"success": False, "status_code": 0, "message": f"Request failed: {str(e)}"}

	def resume_service(self, service_id: str) -> Dict:
		"""החזרת שירות לפעילות"""
		url = f"{self.base_url}/services/{service_id}/resume"

		try:
			response = requests.post(url, headers=self.headers, timeout=15)
			return {
				"success": response.status_code == 200,
				"status_code": response.status_code,
				"message": response.text if response.status_code != 200 else "Service resumed successfully",
			}
		except requests.RequestException as e:
			return {"success": False, "status_code": 0, "message": f"Request failed: {str(e)}"}

	def get_service_info(self, service_id: str) -> Optional[Dict[str, Any]]:
		"""קבלת מידע על שירות"""
		url = f"{self.base_url}/services/{service_id}"

		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code == 200:
				return cast(Dict[str, Any], response.json())
			return None
		except requests.RequestException:
			return None

	def _get_latest_deploy_status(self, service_id: str) -> Optional[str]:
		"""מחזיר את סטטוס הדיפלוי האחרון עבור שירות אם זמין"""
		url = f"{self.base_url}/services/{service_id}/deploys?limit=1"
		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code != 200:
				return None
			data = cast(Any, response.json())
			# תמיכה במערך גולמי, פריסת נתונים, או עטיפה {deploy: {...}}
			latest: Any
			if isinstance(data, list):
				latest = data[0] if data else None
			elif isinstance(data, dict) and data.get("deploy"):
				latest = data
			else:
				items = cast(Any, data).get("items") or cast(Any, data).get("data") or []
				latest = items[0] if items else None

			if latest and isinstance(latest, dict):
				entity = latest.get("deploy") if isinstance(latest.get("deploy"), dict) else latest
				return cast(Optional[str], entity.get("status") or entity.get("state"))
			return None
		except requests.RequestException:
			return None

	def get_latest_deploy_info(self, service_id: str) -> Optional[Dict[str, Any]]:
		"""מחזיר מידע מפורט על הדיפלוי האחרון של שירות

		מחזיר מילון עם שדות עיקריים: id, status/state, createdAt, updatedAt/finishedAt, commitMessage/commitId
		"""
		url = f"{self.base_url}/services/{service_id}/deploys?limit=1"
		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code != 200:
				return None
			data = cast(Any, response.json())

			# הפוך לרשומות אחידות
			records: List[Dict[str, Any]]
			if isinstance(data, list):
				records = cast(List[Dict[str, Any]], data)
			elif isinstance(data, dict):
				if data.get("deploy") and isinstance(data.get("deploy"), dict):
					records = [cast(Dict[str, Any], data)]
				else:
					records = cast(List[Dict[str, Any]], data.get("items") or data.get("data") or [])
			else:
				records = []

			if not records:
				return None

			def extract_entity(rec: Dict[str, Any]) -> Dict[str, Any]:
				return cast(Dict[str, Any], rec.get("deploy") if isinstance(rec.get("deploy"), dict) else rec)

			def parse_iso(ts: Optional[str]) -> Optional[datetime]:
				if not ts or not isinstance(ts, str):
					return None
				try:
					if ts.endswith("Z"):
						ts = ts.replace("Z", "+00:00")
					return datetime.fromisoformat(ts)
				except Exception:
					return None

			def entity_ts(ent: Dict[str, Any]) -> datetime:
				updated = cast(Optional[str], ent.get("updatedAt") or ent.get("finishedAt") or ent.get("completedAt"))
				created = cast(Optional[str], ent.get("createdAt") or ent.get("created_at"))
				parsed = parse_iso(updated) or parse_iso(created)
				return parsed or datetime.min

			latest_rec = sorted(records, key=lambda r: entity_ts(extract_entity(r)), reverse=True)[0]
			entity = extract_entity(latest_rec)

			deploy_id = entity.get("id") or entity.get("deployId")
			status = cast(Optional[str], entity.get("status") or entity.get("state"))
			created_at = cast(Optional[str], entity.get("createdAt") or entity.get("created_at"))
			updated_at = cast(
				Optional[str],
				(entity.get("updatedAt") or entity.get("finishedAt") or entity.get("completedAt") or entity.get("updated_at")),
			)
			commit_message = None
			commit_id = None
			commit = entity.get("commit") or {}
			if isinstance(commit, dict):
				commit_message = cast(Optional[str], commit.get("message") or commit.get("title"))
				commit_id = cast(Optional[str], commit.get("id") or commit.get("sha"))
			else:
				commit_message = entity.get("commitMessage") or entity.get("message")
				commit_id = entity.get("commitId") or entity.get("commit")

			return {
				"id": deploy_id,
				"status": status,
				"createdAt": created_at,
				"updatedAt": updated_at,
				"commitMessage": commit_message,
				"commitId": commit_id,
				"raw": entity,
			}
		except requests.RequestException:
			return None

	def get_service_status(self, service_id: str) -> Optional[str]:
		"""קבלת סטטוס שירות עדכני
		עדיפות למידע שירות חי (online/offline/suspended).
		משתמש בסטטוס דיפלוי רק כדי לציין מצב 'deploying',
		כדי להימנע מסיווג שגוי כ-offline כשדיפלוי נכשל אך הגרסה הקודמת עדיין פועלת.
		"""
		# קודם כל ננסה להביא מידע שירות חי
		service_info = self.get_service_info(service_id)
		if isinstance(service_info, dict) and service_info:
			# אינדיקציית השעיה מפורשת
			if service_info.get("suspended") is True or service_info.get("suspenders"):
				return "suspended"
			# סטטוס/מצב ישיר מהאובייקט
			status = cast(Optional[str], service_info.get("status") or service_info.get("state"))
			if status:
				return status

		# אם לא קיבלנו סטטוס ברור, נבדוק סטטוס דיפלוי
		deploy_status = self._get_latest_deploy_status(service_id)
		if deploy_status:
			# נשתמש בדיפלוי רק כדי לשקף 'deploying'
			lower = str(deploy_status).lower()
			if any(
				k in lower
				for k in [
					"deploy",
					"build",
					"progress",
					"start",
					"provision",
					"pending",
					"queue",
					"updat",
					"initializ",
					"restarting",
				]
			):
				return "deploying"
			# מצבי סיום כמו failed/succeeded אינם משקפים בהכרח מצב ריצה נוכחי
			# ולכן לא נקבע בהם online/offline כאן.

		return "unknown"

	def list_services(self) -> List[Dict[str, Any]]:
		"""רשימת כל השירותים"""
		url = f"{self.base_url}/services"

		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code != 200:
				return []

			data = response.json()
			services: List[Dict[str, Any]] = []

			def _as_service(entity: Any) -> None:
				"""הוספת ישות כשירות לאחר הסרה של שכבת עטיפה אם קיימת"""
				if not isinstance(entity, dict):
					return
				obj: Any = entity
				# פריסה אם יש מפתח "service" שמכיל את האובייקט בפועל
				inner = obj.get("service") if isinstance(obj.get("service"), dict) else None
				if inner:
					obj = inner

				# אם זה נראה כמו אובייקט שירות, הוסף
				if isinstance(obj, dict):
					services.append(cast(Dict[str, Any], obj))

			# טיפול במבנים שונים שמוחזרים מה-API
			if isinstance(data, list):
				for item in data:
					_as_service(item)
			elif isinstance(data, dict):
				for key in ("items", "data", "services", "result", "list"):
					val = data.get(key)
					if isinstance(val, list):
						for item in val:
							_as_service(item)
						break
				else:
					# ייתכן שזה אובייקט יחיד של שירות
					_as_service(data)

			return services
		except requests.RequestException:
			return []

	def get_suspended_services(self) -> list:
		"""רשימת שירותים מושעים"""
		services = self.list_services()
		return [service for service in services if service.get("status") == "suspended" or service.get("suspended") is True]

	# ===== מידע על דיסקים ותוכניות תמחור =====

	def list_disks(self) -> List[Dict[str, Any]]:
		"""מחזיר רשימת דיסקים (Persistent Disks) אם נתמכים ב-API

		החזרה: רשימה של אובייקטים עם שדות כגון: id, serviceId, sizeGB, mountPath.
		במקרה של תקלה או אם ה-API אינו זמין - מוחזרת רשימה ריקה.
		"""
		url = f"{self.base_url}/disks"
		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code == 200:
				data = response.json()
				if isinstance(data, list):
					return cast(List[Dict[str, Any]], data)
				items = cast(Any, data).get("items") or cast(Any, data).get("data")
				if isinstance(items, list):
					return cast(List[Dict[str, Any]], items)
			return []
		except requests.RequestException:
			return []

	def service_has_disk(self, service: Dict[str, Any]) -> bool:
		"""נסה לזהות אם לשירות יש דיסק קבוע לפי מבנה ה-JSON.

		בודק שדות אפשריים: disk, disks, persistentDisk, volumes וכן תחת serviceDetails/spec/details.
		"""
		candidates: List[Any] = []
		for key in ("disk", "disks", "persistentDisk", "volumes"):
			val = service.get(key)
			if val is not None:
				candidates.append(val)
		service_details = cast(Optional[Dict[str, Any]], service.get("serviceDetails"))
		if isinstance(service_details, dict):
			for key in ("disk", "disks", "persistentDisk", "volumes"):
				val = service_details.get(key)
				if val is not None:
					candidates.append(val)
		for path in (("spec", "disk"), ("spec", "disks"), ("details", "disk"), ("details", "disks")):
			val2 = self._extract_nested(service, *path)
			if val2 is not None:
				candidates.append(val2)

		for cand in candidates:
			# אם זו רשימה של דיסקים
			if isinstance(cand, list) and len(cand) > 0:
				return True
			# אם זה מילון שמכיל mountPath/sizeGB
			if isinstance(cand, dict) and ("mountPath" in cand or "sizeGB" in cand or "size" in cand):
				return True
			# מחרוזת לא מספיקה לזיהוי
		return False

	def _extract_nested(self, data: Dict[str, Any], *keys: str) -> Optional[Any]:
		"""עוזר: מחלץ מפתח מקונן אם קיים (לפי רצף מפתחות)."""
		current: Any = data
		for key in keys:
			if not isinstance(current, dict):
				return None
			current = current.get(key)
		return cast(Optional[Any], current)

	def get_service_plan_string(self, service: Dict[str, Any]) -> Optional[str]:
		"""מנסה להפיק את שם התוכנית/תמחור של השירות מתוך אובייקט השירות.

		בודק שדות אפשריים שונים כדי להיות חסין לשינויים ב-API: plan, tier, instanceType, וכן
		תחת serviceDetails.* אם קיים.
		"""
		candidates: List[Optional[str]] = []
		for key in ("plan", "tier", "instanceType"):
			val = service.get(key)
			if isinstance(val, str) and val.strip():
				candidates.append(val)
		# בדיקה תחת serviceDetails
		service_details = cast(Optional[Dict[str, Any]], service.get("serviceDetails"))
		if isinstance(service_details, dict):
			for key in ("plan", "tier", "instanceType"):
				val = service_details.get(key)
				if isinstance(val, str) and val.strip():
					candidates.append(val)

		# נסה גם תחת spec/details אם קיים
		for path in (("spec", "plan"), ("spec", "tier"), ("details", "plan"), ("details", "tier")):
			val2 = self._extract_nested(service, *path)
			if isinstance(val2, str) and val2.strip():
				candidates.append(val2)

		for c in candidates:
			lower = c.lower()
			# נקה ערכים נפוצים
			if any(k in lower for k in ["free", "starter", "standard", "pro", "plus"]):
				return c
		# אם לא זוהה מפתח ברור אך יש ערך כלשהו, החזר ראשון
		return candidates[0] if candidates else None

	def is_free_plan(self, plan: Optional[str]) -> Optional[bool]:
		"""קובע אם התוכנית היא חינמית על בסיס שם התוכנית.

		מחזיר True אם מזוהה 'free', False אם מזוהה תוכנית אחרת מוכרת, None אם לא ידוע.
		"""
		if not plan or not isinstance(plan, str):
			return None
		lower = plan.lower().strip()
		if "free" in lower:
			return True
		if any(k in lower for k in ["starter", "standard", "pro", "plus", "team", "business", "enterprise"]):
			return False
		return None

	# ===== משתני סביבה =====

	def get_env_vars(self, service_id: str) -> List[Dict[str, Any]]:
		"""קבלת רשימת משתני הסביבה של שירות
		
		Returns:
			רשימת אובייקטי env var, כל אחד עם: key, value (אם לא סודי)
		"""
		url = f"{self.base_url}/services/{service_id}/env-vars"
		
		try:
			response = requests.get(url, headers=self.headers, timeout=15)
			if response.status_code == 200:
				data = response.json()
				# טיפול במבני JSON שונים
				if isinstance(data, list):
					return cast(List[Dict[str, Any]], data)
				elif isinstance(data, dict):
					# חיפוש במפתחות מוכרים
					for key in ("envVars", "env_vars", "data", "items", "result"):
						val = data.get(key)
						if isinstance(val, list):
							return cast(List[Dict[str, Any]], val)
			return []
		except requests.RequestException as e:
			import logging
			logging.error(f"Error fetching env vars for service {service_id}: {e}")
			return []

	def update_env_var(self, service_id: str, key: str, value: str) -> Dict[str, Any]:
		"""עדכון או הוספת משתנה סביבה בודד לשירות
		
		Args:
			service_id: מזהה השירות
			key: שם המשתנה
			value: ערך המשתנה
		
		Returns:
			מילון עם success, status_code, message
		"""
		url = f"{self.base_url}/services/{service_id}/env-vars/{key}"
		
		payload = {"value": value}
		
		try:
			# נסה PATCH תחילה (עדכון)
			response = requests.patch(url, headers=self.headers, json=payload, timeout=15)
			
			if response.status_code in [200, 201]:
				return {
					"success": True,
					"status_code": response.status_code,
					"message": f"Environment variable '{key}' updated successfully"
				}
			elif response.status_code == 404:
				# המשתנה לא קיים, ננסה ליצור
				create_url = f"{self.base_url}/services/{service_id}/env-vars"
				create_payload = {"key": key, "value": value}
				create_response = requests.post(create_url, headers=self.headers, json=create_payload, timeout=15)
				
				if create_response.status_code in [200, 201]:
					return {
						"success": True,
						"status_code": create_response.status_code,
						"message": f"Environment variable '{key}' created successfully"
					}
				else:
					return {
						"success": False,
						"status_code": create_response.status_code,
						"message": f"Failed to create env var: {create_response.text}"
					}
			else:
				return {
					"success": False,
					"status_code": response.status_code,
					"message": f"Failed to update: {response.text}"
				}
		except requests.RequestException as e:
			return {
				"success": False,
				"status_code": 0,
				"message": f"Request failed: {str(e)}"
			}

	def delete_env_var(self, service_id: str, key: str) -> Dict[str, Any]:
		"""מחיקת משתנה סביבה משירות
		
		Args:
			service_id: מזהה השירות
			key: שם המשתנה למחיקה
		
		Returns:
			מילון עם success, status_code, message
		"""
		url = f"{self.base_url}/services/{service_id}/env-vars/{key}"
		
		try:
			response = requests.delete(url, headers=self.headers, timeout=15)
			
			if response.status_code in [200, 204]:
				return {
					"success": True,
					"status_code": response.status_code,
					"message": f"Environment variable '{key}' deleted successfully"
				}
			else:
				return {
					"success": False,
					"status_code": response.status_code,
					"message": f"Failed to delete: {response.text}"
				}
		except requests.RequestException as e:
			return {
				"success": False,
				"status_code": 0,
				"message": f"Request failed: {str(e)}"
			}

	# ===== לוגים =====

	def get_service_logs(self, service_id: str, tail: int = 100, start_time: Optional[str] = None,
	                    end_time: Optional[str] = None) -> List[Dict[str, Any]]:
		"""קבלת לוגים של שירות
		
		Args:
			service_id: מזהה השירות
			tail: מספר שורות לוג להחזיר (ברירת מחדל: 100, מקסימום: 10000)
			start_time: זמן התחלה (ISO 8601 format)
			end_time: זמן סיום (ISO 8601 format)
		
		Returns:
			רשימת entries של לוגים, כל אחד עם: id, timestamp, text, stream
		"""
		url = f"{self.base_url}/services/{service_id}/logs"
		
		# Render API uses 'limit', 'start', 'end'
		params = {}
		if tail is not None:
			params["limit"] = min(max(tail, 0), 500) 
		if start_time:
			params["start"] = start_time
		if end_time:
			params["end"] = end_time

		def _parse_logs_payload(payload: Any) -> List[Dict[str, Any]]:
			def _looks_like_entry(node: Any) -> bool:
				if not isinstance(node, dict):
					return False
				text_candidate = None
				for key in ("text", "message", "log", "body", "line"):
					if node.get(key) is not None:
						text_candidate = node.get(key)
						break
				if text_candidate is None:
					return False
				for marker in ("timestamp", "time", "ts", "id", "logId", "stream", "type", "level", "severity"):
					if marker in node:
						return True
				return False

			def _collect_entries(node: Any) -> List[Dict[str, Any]]:
				if isinstance(node, list):
					collected: List[Dict[str, Any]] = []
					for item in node:
						collected.extend(_collect_entries(item))
					return collected
				if isinstance(node, dict):
					if _looks_like_entry(node):
						return [node]
					collected: List[Dict[str, Any]] = []
					for value in node.values():
						collected.extend(_collect_entries(value))
					return collected
				return []

			if isinstance(payload, list):
				return _collect_entries(payload)
			if isinstance(payload, dict):
				for key in ("logs", "entries", "data", "items", "result", "records", "logEntries", "log_entries"):
					val = payload.get(key)
					if isinstance(val, list):
						extracted = _collect_entries(val)
						if extracted:
							return extracted
				for key in ("logGroups", "log_groups", "groups"):
					groups = payload.get(key)
					if isinstance(groups, list):
						collected: List[Dict[str, Any]] = []
						for group in groups:
							collected.extend(_collect_entries(group))
						if collected:
							return collected
				inner = payload.get("log") or payload.get("response")
				if isinstance(inner, dict):
					result = _collect_entries(inner)
					if result:
						return result
			return _collect_entries(payload)

		def _normalize_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
			normalized: List[Dict[str, Any]] = []
			for entry in entries:
				if not isinstance(entry, dict):
					continue
				text = entry.get("text") or entry.get("message") or entry.get("log") or entry.get("body")
				if text is None:
					continue
				if not isinstance(text, str):
					text = str(text)
				stream = entry.get("stream") or entry.get("type") or entry.get("channel")
				if isinstance(stream, str):
					lower = stream.lower()
					if "err" in lower:
						stream = "stderr"
					elif "out" in lower:
						stream = "stdout"
				if not stream:
					level = entry.get("level") or entry.get("severity")
					if isinstance(level, str) and "err" in level.lower():
						stream = "stderr"
				if not stream:
					stream = "stdout"
				timestamp = entry.get("timestamp") or entry.get("time") or entry.get("ts")
				if timestamp is not None and not isinstance(timestamp, str):
					timestamp = str(timestamp)
				log_id = entry.get("id") or entry.get("logId") or entry.get("_id") or entry.get("uuid")
				normalized.append(
					{
						"id": log_id,
						"timestamp": timestamp,
						"text": text,
						"stream": stream,
						"raw": entry,
					}
				)
			return normalized

		import logging
		try:
			# First attempt with standard parameters
			resp = requests.get(url, headers=self.headers, params=params, timeout=30)
			if resp.status_code == 200:
				logs = _normalize_entries(_parse_logs_payload(resp.json()))
				if logs:
					return logs
			
			# Fallback to legacy parameters if first attempt failed or returned no logs
			legacy_params = {}
			if tail is not None:
				legacy_params["tail"] = min(max(tail, 0), 10000)
			if start_time:
				legacy_params["startTime"] = start_time
			if end_time:
				legacy_params["endTime"] = end_time
				
			resp2 = requests.get(url, headers=self.headers, params=legacy_params, timeout=30)
			if resp2.status_code == 200:
				return _normalize_entries(_parse_logs_payload(resp2.json()))
				
			logging.warning(
				f"Failed to fetch logs for {service_id}. codes: {resp.status_code}, {resp2.status_code if 'resp2' in locals() else 'N/A'}"
			)
			return []
		except requests.RequestException as e:
			logging.error(f"Error fetching logs for service {service_id}: {e}")
			return []

	def get_recent_logs(self, service_id: str, minutes: int = 5) -> List[Dict[str, Any]]:
		"""קבלת לוגים מהדקות האחרונות
		
		Args:
			service_id: מזהה השירות
			minutes: כמה דקות אחורה לחפש
		
		Returns:
			רשימת לוגים
		"""
		from datetime import datetime, timezone, timedelta
		
		try:
			# Use explicit start time
			start_dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
			start_str = start_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
			
			# Use generous limit
			logs = self.get_service_logs(service_id, tail=500, start_time=start_str)
			
			if not logs:
				logs = self.get_service_logs(service_id, tail=500)
			
			if not logs:
				return []

			# Filter again to ensure time range
			end_time = datetime.now(timezone.utc)
			start_time = end_time - timedelta(minutes=minutes)
			filtered: List[Dict[str, Any]] = []
			for entry in logs:
				ts_raw = entry.get("timestamp")
				if not isinstance(ts_raw, str):
					continue
				try:
					iso = ts_raw.replace("Z", "+00:00") if isinstance(ts_raw, str) else ts_raw
					ts = datetime.fromisoformat(iso)
				except Exception:
					filtered.append(entry)
					continue
				if start_time <= ts <= end_time:
					filtered.append(entry)
			records = filtered if filtered else logs
			return records
		except Exception:
			return []

# יצירת instance גלובלי
render_api = RenderAPI()