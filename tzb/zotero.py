"""Client for the Zotero 10 Local API (http://localhost:23119/api)."""
import json
import urllib.error
import urllib.parse
import urllib.request

ROOT = "http://localhost:23119/api"


class ZoteroError(Exception):
    pass


def _call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req) as r:
            status, h, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, h, raw = e.code, e.headers, e.read()
    if "json" in (h.get("Content-Type") or "") and raw:
        return status, h, json.loads(raw)
    return status, h, raw.decode()


def server_id():
    return _call("GET", ROOT + "/")[1]["Zotero-Server-ID"]


def authorize(app_name="tine-zotero"):
    """Ask Zotero for a write key; the user must click Allow in Zotero. Returns {"server_id", "api_key"}."""
    sid = server_id()
    status, _, body = _call("POST", ROOT + "/local/authorize", {"appName": app_name}, {"Zotero-Server-ID": sid})
    if status != 200 or not isinstance(body, dict) or "key" not in body:
        raise ZoteroError(f"authorize: {status} {body}")
    return {"server_id": sid, "api_key": body["key"]}


class Zotero:
    def __init__(self, auth=None, library="/users/0"):
        self.base = ROOT + library
        self.auth = auth    # {"server_id", "api_key"}; needed only for writes

    def get(self, path):
        status, h, body = _call("GET", self.base + path)
        if status != 200:
            raise ZoteroError(f"GET {path}: {status} {body}")
        return body, h

    def get_all(self, path):
        """All pages of a list endpoint -> (items, Last-Modified-Version of the first page)."""
        items, version = [], None
        while True:
            sep = "&" if "?" in path else "?"
            page, h = self.get(f"{path}{sep}limit=100&start={len(items)}")
            version = version if version is not None else int(h["Last-Modified-Version"])
            items += page
            if not page or len(items) >= int(h["Total-Results"]):
                return items, version

    def library_version(self):
        return int(self.get("/items?limit=1")[1]["Last-Modified-Version"])

    def file_path(self, att_key):
        url = self.get(f"/items/{att_key}/file/view/url")[0].strip()
        return urllib.parse.unquote(url.removeprefix("file://"))

    def _write(self, method, path, body=None, version=None):
        h = {"Zotero-API-Key": self.auth["api_key"], "Zotero-Server-ID": self.auth["server_id"]}
        if version is not None:
            h["If-Unmodified-Since-Version"] = str(version)
        return _call(method, self.base + path, body, h)

    def create(self, item):
        """POST one new item; returns its key."""
        status, _, body = self._write("POST", "/items", [item])
        if status != 200 or not body.get("success"):
            raise ZoteroError(f"create: {status} {body}")
        return body["success"]["0"]

    def patch(self, key, data, version):
        """PATCH with a version check. Returns False on 412 (changed in Zotero since `version`)."""
        status, _, body = self._write("PATCH", f"/items/{key}", data, version)
        if status == 412:
            return False
        if status != 204:
            raise ZoteroError(f"PATCH {key}: {status} {body}")
        return True

    def delete(self, key, version):
        """DELETE with a version check (permanent: annotations skip the Zotero trash). False on 412."""
        status, _, body = self._write("DELETE", f"/items/{key}", version=version)
        if status == 412:
            return False
        if status != 204:
            raise ZoteroError(f"DELETE {key}: {status} {body}")
        return True
