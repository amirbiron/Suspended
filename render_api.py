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
        """קבלת סטטוס שירות"""
        service_info = self.get_service_info(service_id)
        if service_info:
            # Render services לרוב כוללים שדה 'suspended' בוליאני, ולא תמיד 'status'
            if "status" in service_info and service_info["status"]:
                return service_info["status"]
            if "suspended" in service_info:
                return "suspended" if service_info.get("suspended") else "active"
            return "unknown"
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
