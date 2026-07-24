"""
This class replaces database_queries.py to act as a proxy between the POSSUM database and the code.
We can no longer directly query the database, so we use the REST API instead.
"""
import os

import requests

from dotenv import load_dotenv
from pathlib import Path
from prefect.blocks.system import Secret

class PossumApiClient:

    def __init__(self, config_file_path: str = None):
        if getattr(self, "_initialized", False):
            return

        self.base_url = None
        self.username = None
        self.password = None

        config_file = Path(config_file_path) if config_file_path else None

        if config_file is not None and config_file.exists():
            # if config.env is supplied, we'll use the variables from the file
            load_dotenv(config_file)
            self.base_url = os.environ.get("POSSUM_API_URL")
            self.username = os.environ.get("POSSUM_API_USERNAME")
            self.password = os.environ.get("POSSUM_API_PASSWORD")

        if not self.base_url:
            # otherwise load from Prefect secrets
            self.base_url = Secret.load("possum-api-url", _sync=True).get()
        if not self.username or not self.password:
            self.username = Secret.load("possum-api-username", _sync=True).get()
            self.password = Secret.load("possum-api-password", _sync=True).get()

        self.session = requests.Session()

        self.access_token = None
        self.refresh_token = None

        self._initialized = True

    def login(self):
        response = self.session.post(
            url = f"{self.base_url.rstrip('/')}/token/",
            json={
                "username": self.username,
                "password": self.password,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        self.access_token = data["access"]
        self.refresh_token = data["refresh"]

    def refresh_access_token(self):
        if not self.refresh_token:
            self.login()
            return

        response = self.session.post(
            url = f"{self.base_url.rstrip('/')}/token/refresh/",
            json={"refresh": self.refresh_token},
            timeout=30,
        )

        if response.status_code == 401:
            self.login()
            return

        response.raise_for_status()

        self.access_token = response.json()["access"]

    def _ensure_authenticated(self):
        if self.access_token is None:
            self.login()

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
        }

    def _request(self, method, endpoint, **kwargs):
        self._ensure_authenticated()
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

        response = self.session.request(
            method=method,
            url=url, 
            headers=self._headers(),
            timeout=30,
            **kwargs,
        )

        if response.status_code == 401:
            self.refresh_access_token()

            response = self.session.request(
                method=method,
                url=url,
                headers=self._headers(),
                timeout=30,
                **kwargs,
            )

        if not response.ok:
            try:
                error = response.json().get("error", response.text)
            except ValueError:
                error = response.text
            raise requests.HTTPError(
                f"{response.status_code} {response.reason}: {error}",
                response=response,
            )    

        return response

    def get(self, endpoint, **kwargs):
        data = self._request("GET", endpoint, **kwargs).json()
        # return the data as tuples as it was when we queried the DB directly
        return [
            tuple(row.values()) if isinstance(row, dict) else (row,)
            for row in data
        ]
    
    def get_json(self, endpoint, **kwargs):
        # get json as is
        return  self._request("GET", endpoint, **kwargs).json()

    def post(self, endpoint, **kwargs):
        return self._request("POST", endpoint, **kwargs).json()

    def put(self, endpoint, **kwargs):
        return self._request("PUT", endpoint, **kwargs).json()

    def patch(self, endpoint, **kwargs):
        return self._request("PATCH", endpoint, **kwargs).json()

    def delete(self, endpoint, **kwargs):
        return self._request("DELETE", endpoint, **kwargs)
    
