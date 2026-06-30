"""Fetch photos from a public iCloud Shared Album.

Newer photos.icloud.com/shared/album/<token> links are served by CloudKit Web
Services (ckdatabasews), not the legacy `webstream` API. Flow:
  1. POST .../public/records/resolve  {"shortGUIDs":[{"value":token}]}
       -> zoneID, anonymousPublicAccess.{token, databasePartition}, share title
  2. POST <partition>/.../shared/records/query  (CPLAssetAndMasterByAddedDate)
       -> CPLMaster records: resOriginalRes.downloadURL (${f}=filename),
          resOriginalWidth/Height, filenameEnc (base64)
The album must be a public ("anyone with the link") shared album. Stdlib only.
"""
import base64
import json
import urllib.parse
import urllib.request

CONTAINER = "com.apple.photos.cloud"
GATEWAY = "https://ckdatabasews.icloud.com"
PAGE = 100


def token_from_url(s):
    s = s.strip().rstrip("/")
    if "/shared/album/" in s:
        s = s.split("/shared/album/")[1]
    if "#" in s:
        s = s.split("#")[1]
    return s.split("/")[0].split("?")[0]


def _post(url, payload, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "text/plain", "Origin": "https://www.icloud.com",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _resolve(token):
    url = (f"{GATEWAY}/database/1/{CONTAINER}/production/public/records/resolve"
           "?remapEnums=true&getCurrentSyncToken=true")
    res = _post(url, {"shortGUIDs": [{"value": token}]})["results"][0]
    apa = res["anonymousPublicAccess"]
    return {
        "zoneID": res["zoneID"],
        "authToken": apa["token"],
        "host": apa["databasePartition"].replace(":443", "").rstrip("/"),
        "title": res.get("share", {}).get("fields", {}).get("cloudkit.title", {}).get("value", ""),
    }


def _query(ctx, start):
    qs = (f"remapEnums=true&getCurrentSyncToken=true&sharing_url_key={ctx['token']}"
          f"&publicAccessAuthToken={urllib.parse.quote(ctx['authToken'])}")
    url = f"{ctx['host']}/database/1/{CONTAINER}/production/shared/records/query?{qs}"
    body = {"query": {"recordType": "CPLAssetAndMasterByAddedDate", "filterBy": [
        {"fieldName": "direction", "comparator": "EQUALS",
         "fieldValue": {"value": "ASCENDING", "type": "STRING"}},
        {"fieldName": "startRank", "comparator": "EQUALS",
         "fieldValue": {"value": start, "type": "INT64"}}]},
        "zoneID": ctx["zoneID"], "resultsLimit": PAGE}
    return _post(url, body)


def _field(fields, name, default=None):
    return fields.get(name, {}).get("value", default)


def photo_from_master(m):
    """Turn one CPLMaster record into a photo dict, or None if it has no
    downloadable original. Pure (no network), so it is unit-testable."""
    if m.get("recordType") != "CPLMaster":
        return None
    f = m.get("fields", {})
    url = f.get("resOriginalRes", {}).get("value", {}).get("downloadURL", "")
    if not url:
        return None
    enc = _field(f, "filenameEnc", "")
    name = base64.b64decode(enc).decode("utf8", "replace") if enc else m["recordName"]
    return {
        "guid": m["recordName"],
        "filename": name,
        "caption": "",
        "width": _field(f, "resOriginalWidth", 0) or 0,
        "height": _field(f, "resOriginalHeight", 0) or 0,
        "url": url.replace("${f}", urllib.parse.quote(name)),
    }


def fetch_album(url_or_token):
    """Return {'name': str, 'photos': [{guid, filename, caption, width, height, url}]}.

    Pages through the WHOLE album. Each asset comes back as two records (a
    CPLMaster + a CPLAsset), so a full PAGE-record response carries only ~PAGE/2
    photos. The page boundary is therefore a short *record* page, never a short
    *master* page -- the latter was the old bug: ~50 masters per 100-record page
    is always < PAGE, so the loop stopped after page 1 and dropped everything
    past the first ~50 photos. Since the query is added-date ASCENDING, those
    dropped photos are exactly the newest ones, so freshly added album photos
    never reached the frame. startRank advances by the photos seen on each page;
    guids dedupe any page overlap and signal the end.
    """
    ctx = _resolve(token_from_url(url_or_token))
    ctx["token"] = token_from_url(url_or_token)
    photos, seen, start = [], set(), 0
    while start < 100000:                       # safety cap
        records = _query(ctx, start).get("records", [])
        if not records:
            break
        fresh = 0
        masters = 0
        for m in records:
            if m.get("recordType") == "CPLMaster":
                masters += 1
            p = photo_from_master(m)
            if p and p["guid"] not in seen:
                seen.add(p["guid"])
                photos.append(p)
                fresh += 1
        start += masters or len(records)        # advance by photos on this page
        if len(records) < PAGE or fresh == 0:   # short page, or nothing new => done
            break
    return {"name": ctx["title"], "photos": photos}


if __name__ == "__main__":
    import sys
    a = fetch_album(sys.argv[1])
    print(f"album: {a['name']!r}  photos: {len(a['photos'])}")
    for p in a["photos"][:10]:
        print(f"  {p['filename']:18} {p['width']}x{p['height']}  {p['url'][:70]}")
