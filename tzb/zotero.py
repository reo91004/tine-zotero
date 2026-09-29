"""Client for the Zotero 10 Local API (http://localhost:23119/api)."""
import json
import urllib.error
import urllib.parse
import urllib.request

ROOT = "http://localhost:23119/api"
TIMEOUT_S = 30
# Never send local API calls (and the write key) through an HTTP proxy: macOS does not bypass localhost by default.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ZoteroError(Exception):
    pass


class ZoteroAuthError(ZoteroError):
    """401: the write key is gone (e.g. a one-time key was used up) — run `tzb init` again."""


def _call(method, url, body=None, headers=None, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            status, h, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, h, raw = e.code, e.headers, e.read()
    if "json" in (h.get("Content-Type") or "") and raw:
        return status, h, json.loads(raw)
    return status, h, raw.decode()


def server_id():
    return _call("GET", ROOT + "/")[1]["Zotero-Server-ID"]


def authorize(app_name="tine-zotero"):
    """Ask Zotero for a lasting write key; the user must click Always Allow. Returns {"server_id", "api_key"}."""
    sid = server_id()
    status, _, body = _call("POST", ROOT + "/local/authorize", {"appName": app_name}, {"Zotero-Server-ID": sid},
                            timeout=300)
    if status != 200 or not isinstance(body, dict) or "key" not in body:
        raise ZoteroError(f"authorize: {status} {body}")
    if not body.get("remember"):    # "Allow" gives a key that the first write consumes
        raise ZoteroError("Zotero granted one-time access; run `tzb init` again and click Always Allow")
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
        """A whole list endpoint in one response.

        Not paged: the local API sorts by dateModified, so an item edited between two pages would move to page one
        and vanish from the result, which the sync would read as a deletion.
        """
        items, h = self.get(path)
        if "Total-Results" in h and len(items) != int(h["Total-Results"]):
            raise ZoteroError(f"GET {path}: got {len(items)} of {h['Total-Results']} items")
        return items

    def versions(self, path):
        """{key: version} of a list endpoint: a cheap way to see which items changed."""
        sep = "&" if "?" in path else "?"
        return self.get(f"{path}{sep}format=versions")[0]

    def by_keys(self, keys):
        """Full JSON of the given items."""
        keys, out = sorted(keys), []
        for i in range(0, len(keys), 50):
            out += self.get_all("/items?itemKey=" + ",".join(keys[i:i + 50]))
        return [i for i in out if i["key"] in set(keys)]      # the local API also returns their child items

    def exists(self, key):
        """False only if Zotero has no such item at all (items in the trash, or under a trashed parent, exist)."""
        status, _, body = _call("GET", f"{self.base}/items/{key}")
        if status not in (200, 404):
            raise ZoteroError(f"GET /items/{key}: {status} {body}")
        return status == 200

    def library_version(self):
        return int(self.get("/items?limit=1&format=versions")[1]["Last-Modified-Version"])

    def file_path(self, att_key):
        url = self.get(f"/items/{att_key}/file/view/url")[0].strip()
        return urllib.parse.unquote(url.removeprefix("file://"))

    def _write(self, method, path, body=None, version=None):
        h = {"Zotero-API-Key": self.auth["api_key"], "Zotero-Server-ID": self.auth["server_id"]}
        if version is not None:
            h["If-Unmodified-Since-Version"] = str(version)
        status, _, resp = _call(method, self.base + path, body, h)
        if status == 401:
            raise ZoteroAuthError(f"{method} {path}: 401 {resp}")
        return status, resp

    def create(self, item):
        """POST one new item; returns its key."""
        status, body = self._write("POST", "/items", [item])
        if status != 200 or not body.get("success"):
            raise ZoteroError(f"create: {status} {body}")
        return body["success"]["0"]

    def _versioned(self, method, key, body, version):
        status, resp = self._write(method, f"/items/{key}", body, version)
        if status == 412:
            return False
        if status != 204:
            raise ZoteroError(f"{method} {key}: {status} {resp}")
        return True

    def patch(self, key, data, version):
        """PATCH with a version check. Returns False on 412 (changed in Zotero since `version`)."""
        return self._versioned("PATCH", key, data, version)

    def delete(self, key, version):
        """DELETE with a version check (permanent: annotations skip the Zotero trash). False on 412."""
        return self._versioned("DELETE", key, None, version)
