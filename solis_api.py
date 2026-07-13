"""
SolisCloud API client.

Uses the official signed REST API (not scraping). You need API credentials
from SolisCloud -> Service -> API Management (takes 24-48h for Solis to
approve on a new account). This is completely separate from the dashboard
session token in a plant URL -- don't use that token here, it's not the
same thing and it can expire/be revoked at any time.

Uses /v1/api/userStationList rather than /v1/api/stationDetail: both
return real-time power, but stationDetail was unreliable (returned
success:true / data:null on a verified-working account+plant, a known
generic Solis error), while userStationList reliably returns full detail
for every station the API key's account can see, filtered down here to
the one matching SOLIS_PLANT_ID.

Docs: https://oss.soliscloud.com/templet/SolisCloud%20Platform%20API%20Document%20V2.0.2.pdf
"""

import base64
import hashlib
import hmac
import json
import time
from email.utils import formatdate

import requests

API_BASE = "https://www.soliscloud.com:13333"  # standard SolisCloud API host


class SolisClient:
    def __init__(self, key_id: str, key_secret: str, plant_id: str):
        self.key_id = key_id
        self.key_secret = key_secret.encode()
        self.plant_id = str(plant_id)

    def _signed_headers(self, resource: str, body: dict) -> dict:
        body_str = json.dumps(body, separators=(",", ":"))
        content_md5 = base64.b64encode(hashlib.md5(body_str.encode()).digest()).decode()
        content_type = "application/json"
        date = formatdate(timeval=time.time(), usegmt=True)

        string_to_sign = "\n".join(
            ["POST", content_md5, content_type, date, resource]
        )
        sign = base64.b64encode(
            hmac.new(self.key_secret, string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()

        return {
            "Content-MD5": content_md5,
            "Content-Type": content_type,
            "Date": date,
            "Authorization": f"API {self.key_id}:{sign}",
        }, body_str

    def _post(self, resource: str, body: dict) -> dict:
        headers, body_str = self._signed_headers(resource, body)
        resp = requests.post(API_BASE + resource, headers=headers, data=body_str, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") or data.get("data") is None:
            raise RuntimeError(f"SolisCloud API error: {data}")
        return data["data"]

    def get_station_record(self) -> dict:
        """
        Returns the raw record for this plant from userStationList -- includes
        current power ('power', in kW), daily/monthly/yearly energy, alarm
        state, etc. Paginated in theory, but a home install only has one
        station, so page 1 with a generous page size covers it; raises if
        the configured plant_id isn't found in the results.
        """
        resource = "/v1/api/userStationList"
        body = {"pageNo": 1, "pageSize": 20}
        data = self._post(resource, body)
        records = data.get("page", {}).get("records", [])
        for record in records:
            if str(record.get("id")) == self.plant_id:
                return record
        raise RuntimeError(
            f"Plant id {self.plant_id} not found in userStationList results "
            f"(found: {[r.get('id') for r in records]})"
        )

    def get_current_power_kw(self) -> float:
        """Current output power of the plant, in kW ('power' field, unit
        confirmed by the accompanying 'powerStr' field which should read 'kW')."""
        record = self.get_station_record()
        return float(record["power"])

