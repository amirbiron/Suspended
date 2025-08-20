from typing import Any, Dict, List, Optional, cast

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
            # תמיכה גם במערך גולמי וגם במבנים {"items": [...]} או {"data": [...]}
            if isinstance(data, list):
                latest = data[0] if data else None
            else:
                items = cast(Any, data).get("items") or cast(Any, data).get("data") or []
                latest = items[0] if items else None
            if latest and isinstance(latest, dict):
                return cast(Optional[str], latest.get("status") or latest.get("state"))
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
            if isinstance(data, list):
                latest = data[0] if data else None
            else:
                items = cast(Any, data).get("items") or cast(Any, data).get("data") or []
                latest = items[0] if items else None
            if not latest or not isinstance(latest, dict):
                return None
            deploy_id = latest.get("id") or latest.get("deployId")
            status = cast(Optional[str], latest.get("status") or latest.get("state"))
            created_at = cast(Optional[str], latest.get("createdAt") or latest.get("created_at"))
            updated_at = cast(
                Optional[str],
                (latest.get("updatedAt") or latest.get("finishedAt") or latest.get("completedAt") or latest.get("updated_at")),
            )
            # חלק מהשדות עשויים להיות מקוננים
            commit_message = None
            commit_id = None
            commit = latest.get("commit") or {}
            if isinstance(commit, dict):
                commit_message = cast(Optional[str], commit.get("message") or commit.get("title"))
                commit_id = cast(Optional[str], commit.get("id") or commit.get("sha"))
            else:
                # נסיון חלופי
                commit_message = latest.get("commitMessage") or latest.get("message")
                commit_id = latest.get("commitId") or latest.get("commit")

            return {
                "id": deploy_id,
                "status": status,
                "createdAt": created_at,
                "updatedAt": updated_at,
                "commitMessage": commit_message,
                "commitId": commit_id,
                "raw": latest,
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
