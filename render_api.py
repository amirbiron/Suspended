import requests
import config
from typing import Dict, Optional

class RenderAPI:
    def __init__(self):
        self.api_key = config.RENDER_API_KEY
        self.base_url = config.RENDER_API_URL
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
    
    def suspend_service(self, service_id: str) -> Dict:
        """השעיית שירות"""
        url = f"{self.base_url}/services/{service_id}/suspend"
        
        try:
            response = requests.post(url, headers=self.headers)
            return {
                "success": response.status_code == 200,
                "status_code": response.status_code,
                "message": response.text if response.status_code != 200 else "Service suspended successfully"
            }
        except requests.RequestException as e:
            return {
                "success": False,
                "status_code": 0,
                "message": f"Request failed: {str(e)}"
            }
    
    def resume_service(self, service_id: str) -> Dict:
        """החזרת שירות לפעילות"""
        url = f"{self.base_url}/services/{service_id}/resume"
        
        try:
            response = requests.post(url, headers=self.headers)
            return {
                "success": response.status_code == 200,
                "status_code": response.status_code,
                "message": response.text if response.status_code != 200 else "Service resumed successfully"
            }
        except requests.RequestException as e:
            return {
                "success": False,
                "status_code": 0,
                "message": f"Request failed: {str(e)}"
            }
    
    def get_service_info(self, service_id: str) -> Optional[Dict]:
        """קבלת מידע על שירות"""
        url = f"{self.base_url}/services/{service_id}"
        
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                return response.json()
            return None
        except requests.RequestException:
            return None
    
    def _get_latest_deploy_status(self, service_id: str) -> Optional[str]:
        """מחזיר את סטטוס הדיפלוי האחרון עבור שירות אם זמין"""
        url = f"{self.base_url}/services/{service_id}/deploys?limit=1"
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code != 200:
                return None
            data = response.json()
            # תמיכה גם במערך גולמי וגם במבנים {"items": [...]} או {"data": [...]} 
            if isinstance(data, list):
                latest = data[0] if data else None
            else:
                items = data.get("items") or data.get("data") or []
                latest = items[0] if items else None
            if latest and isinstance(latest, dict):
                return latest.get("status") or latest.get("state")
            return None
        except requests.RequestException:
            return None
    
    def get_service_status(self, service_id: str) -> Optional[str]:
        """קבלת סטטוס שירות
        מנסה קודם את סטטוס הדיפלוי האחרון, ונופל חזרה למידע שירות כולל דגל השעיה.
        """
        # קודם ננסה להביא את סטטוס הדיפלוי האחרון
        deploy_status = self._get_latest_deploy_status(service_id)
        if deploy_status:
            return deploy_status
        
        # נפילה חזרה למידע שירות כללי
        service_info = self.get_service_info(service_id)
        if service_info:
            # אם קיים שדה סטטוס, נחזיר אותו
            if isinstance(service_info, dict):
                status = service_info.get("status") or service_info.get("state")
                if status:
                    return status
                # היגיון נוסף: אם השירות מושעה, נחזיר "suspended" כדי למפות ל-offline
                if service_info.get("suspended") is True:
                    return "suspended"
                # ייתכנו מצבים שבהם אין סטטוס אבל יש אינדיקציה לפעילות
                if service_info.get("suspenders"):
                    return "suspended"
        # אם לא הצלחנו לקבוע סטטוס
        return "unknown"
    
    def list_services(self) -> list:
        """רשימת כל השירותים"""
        url = f"{self.base_url}/services"
        
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                return response.json()
            return []
        except requests.RequestException:
            return []
    
    def get_suspended_services(self) -> list:
        """רשימת שירותים מושעים"""
        services = self.list_services()
        return [
            service for service in services 
            if service.get("status") == "suspended" or service.get("suspended") is True
        ]

# יצירת instance גלובלי
render_api = RenderAPI()
