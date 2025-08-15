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
    
    def get_service_status(self, service_id: str) -> Optional[str]:
        """קבלת סטטוס שירות בצורה סבילה יותר
        נסיון לזהות סטטוס ממספר שדות ונקודות קצה:
        - אם השירות מושעה: החזרת "suspended"
        - אם יש שדה status/state בשירות – החזר אותו
        - נסיון להביא את הסטטוס של ה-Deploy האחרון
        - נסיון להביא סטטוס מאינסטנס ראשון
        """
        service_info = self.get_service_info(service_id)
        if not service_info:
            return None

        # 1) Suspended flag at service level
        try:
            suspended_flag = service_info.get("suspended")
            if isinstance(suspended_flag, bool) and suspended_flag:
                return "suspended"
        except Exception:
            pass

        # 2) Direct status-like fields on the service object
        for key in ("status", "state", "serviceStatus"):
            value = service_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        # 3) Latest deploy status
        try:
            url = f"{self.base_url}/services/{service_id}/deploys?limit=1"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                deploys = response.json()
                if isinstance(deploys, list) and deploys:
                    latest = deploys[0] or {}
                    for key in ("status", "state"):
                        v = latest.get(key)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
        except requests.RequestException:
            pass

        # 4) Instances status as a last resort
        try:
            url = f"{self.base_url}/services/{service_id}/instances"
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                instances = response.json()
                if isinstance(instances, list) and instances:
                    instance = instances[0] or {}
                    for key in ("status", "state", "health"):
                        v = instance.get(key)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
        except requests.RequestException:
            pass

        return None
    
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
            if service.get("status") == "suspended"
        ]

# יצירת instance גלובלי
render_api = RenderAPI()
