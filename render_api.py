from typing import Any, Dict, List, Optional, cast
from datetime import datetime

import requests

import config


class RenderAPI:
	def __init__(self):
		self.api_key = config.RENDER_API_KEY
		self.base_url = config.RENDER_API_URL
		self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

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
			if response.status_code == 200:
				return cast(List[Dict[str, Any]], response.json())
			return []
		except requests.RequestException:
			return []

	def get_suspended_services(self) -> list:
		"""רשימת שירותים מושעים"""
		services = self.list_services()
		return [service for service in services if service.get("status") == "suspended" or service.get("suspended") is True]


# יצירת instance גלובלי
render_api = RenderAPI()