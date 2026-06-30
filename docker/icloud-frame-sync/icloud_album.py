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


# itemType / resOriginalFileType substrings that mark a master as a video.
VIDEO_TYPE_HINTS = ("movie", "video", "mpeg-4")


def _res_url(fields, res_key, dl_name):
    """downloadURL for a CloudKit resource field, ${f} -> dl_name, or '' if absent."""
    v = fields.get(res_key, {}).get("value", {})
    url = v.get("downloadURL", "") if isinstance(v, dict) else ""
    return url.replace("${f}", urllib.parse.quote(dl_name)) if url else ""


def asset_from_master(m):
    """Turn one CPLMaster record into an asset dict, or None if nothing is
    downloadable. Pure (no network), so it is unit-testable.

    Photos use the full original (resOriginalRes). Videos use Apple's H.264 mp4
    derivative -- resVidMedRes (~720p), else resVidSmallRes (~360p) -- never the
    huge HEVC original the RK3126C can't hardware-decode; plus a JPEG poster
    (resJPEGMedRes, else resJPEGThumbRes) for the frame's video thumbnail. The
    returned dict carries a 'kind' ('photo'|'video') and 'poster_url' ('' for
    photos)."""
    if m.get("recordType") != "CPLMaster":
        return None
    f = m.get("fields", {})
    # A true video's itemType is a movie UTI (com.apple.quicktime-movie,
    # public.mpeg-4, ...). Live Photos are *images* that also carry resVid*
    # fields for their motion, so key off itemType -- not resVid* presence --
    # to keep Live Photos as stills.
    itemtype = (_field(f, "itemType", "") or _field(f, "resOriginalFileType", "") or "").lower()
    is_video = any(h in itemtype for h in VIDEO_TYPE_HINTS)
    enc = _field(f, "filenameEnc", "")
    name = base64.b64decode(enc).decode("utf8", "replace") if enc else m["recordName"]

    if is_video:
        url, width, height = "", 0, 0
        for res in ("resVidMedRes", "resVidSmallRes"):
            url = _res_url(f, res, "public.mp4")
            if url:
                width = _field(f, res.replace("Res", "Width"), 0) or 0
                height = _field(f, res.replace("Res", "Height"), 0) or 0
                break
        if not url:
            return None
        poster = _res_url(f, "resJPEGMedRes", "public.jpeg") \
            or _res_url(f, "resJPEGThumbRes", "public.jpeg")
        return {"guid": m["recordName"], "kind": "video", "filename": name,
                "caption": "", "width": width, "height": height,
                "url": url, "poster_url": poster}

    url = _res_url(f, "resOriginalRes", name)
    if not url:
        return None
    return {
        "guid": m["recordName"],
        "kind": "photo",
        "filename": name,
        "caption": "",
        "width": _field(f, "resOriginalWidth", 0) or 0,
        "height": _field(f, "resOriginalHeight", 0) or 0,
        "url": url,
        "poster_url": "",
    }


def fetch_album(url_or_token):
    """Return {'name': str, 'assets': [{guid, kind, filename, caption, width,
    height, url, poster_url}]} -- photos and videos alike.

    Pages through the WHOLE album. Each asset comes back as two records (a
    CPLMaster + a CPLAsset), so a full PAGE-record response carries only ~PAGE/2
    assets. The page boundary is therefore a short *record* page, never a short
    *master* page -- the latter was the old bug: ~50 masters per 100-record page
    is always < PAGE, so the loop stopped after page 1 and dropped everything
    past the first ~50 assets. Since the query is added-date ASCENDING, those
    dropped assets are exactly the newest ones, so freshly added album items
    never reached the frame. startRank advances by the assets seen on each page;
    guids dedupe any page overlap and signal the end.
    """
    ctx = _resolve(token_from_url(url_or_token))
    ctx["token"] = token_from_url(url_or_token)
    assets, seen, start = [], set(), 0
    while start < 100000:                       # safety cap
        records = _query(ctx, start).get("records", [])
        if not records:
            break
        fresh = 0
        masters = 0
        for m in records:
            if m.get("recordType") == "CPLMaster":
                masters += 1
            a = asset_from_master(m)
            if a and a["guid"] not in seen:
                seen.add(a["guid"])
                assets.append(a)
                fresh += 1
        start += masters or len(records)        # advance by assets on this page
        if len(records) < PAGE or fresh == 0:   # short page, or nothing new => done
            break
    return {"name": ctx["title"], "assets": assets}


if __name__ == "__main__":
    import sys
    a = fetch_album(sys.argv[1])
    print(f"album: {a['name']!r}  assets: {len(a['assets'])}")
    for p in a["assets"][:10]:
        print(f"  [{p['kind'][:5]:5}] {p['filename']:18} {p['width']}x{p['height']}  {p['url'][:64]}")
