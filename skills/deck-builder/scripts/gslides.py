# -*- coding: utf-8 -*-
"""
gslides.py — Google Drive/Workspace helpers for shipping decks & docs.

Requires ADC with `drive` + `presentations` scope, e.g.:
    gcloud auth application-default login --scopes=openid,\
https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/drive,\
https://www.googleapis.com/auth/presentations

CLI:
    # create a Google Slides from a pptx, inside a Drive folder
    python gslides.py up-slides deck.pptx FOLDER_ID "Deck name"
    # update that SAME slides in place (link unchanged)
    python gslides.py patch-slides deck.pptx FILE_ID
    # create / update a Google Doc from an HTML file
    python gslides.py up-doc page.html FOLDER_ID "Doc name"
    python gslides.py patch-doc page.html FILE_ID
    # export a Slides/Doc to PDF (for the render self-check)
    python gslides.py export PDF_OUT FILE_ID
    # list a folder
    python gslides.py ls FOLDER_ID

Programmatic: `from gslides import token, create, patch, export, listdir`.

KEY IDEA: keep the returned fileId and PATCH it forever — the share link is
stable, so collaborators always see the latest version.
"""
import json, os, sys, urllib.request, urllib.parse

ADC = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
X_GOOG_USER_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "chris-pgp-host")
DOC  = "application/vnd.google-apps.document"
DECK = "application/vnd.google-apps.presentation"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

def token():
    d = json.load(open(ADC))
    body = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
    r = json.loads(urllib.request.urlopen(
        "https://oauth2.googleapis.com/token", data=body).read().decode())
    return r["access_token"]

def _H():
    return {"Authorization": f"Bearer {token()}",
            "X-Goog-User-Project": X_GOOG_USER_PROJECT}

def create(local_path, folder_id, name, target_mime, src_mime):
    """Upload a local file, converting to a Google-native type. Returns fileId."""
    H = _H()
    meta = {"name": name, "mimeType": target_mime, "parents": [folder_id]}
    init = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files"
        "?uploadType=resumable&supportsAllDrives=true",
        data=json.dumps(meta).encode(),
        headers={**H, "Content-Type": "application/json",
                 "X-Upload-Content-Type": src_mime})
    up = urllib.request.urlopen(init).headers["Location"]
    data = open(local_path, "rb").read()
    r = json.loads(urllib.request.urlopen(
        urllib.request.Request(up, data=data, method="PUT"), timeout=600).read().decode())
    return r["id"]

def patch(local_path, file_id, src_mime):
    """Replace the content of an existing file IN PLACE (link unchanged)."""
    H = _H()
    init = urllib.request.Request(
        f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
        "?uploadType=resumable&supportsAllDrives=true",
        data=b"{}", method="PATCH",
        headers={**H, "Content-Type": "application/json",
                 "X-Upload-Content-Type": src_mime})
    up = urllib.request.urlopen(init).headers["Location"]
    data = open(local_path, "rb").read()
    r = json.loads(urllib.request.urlopen(
        urllib.request.Request(up, data=data, method="PUT"), timeout=600).read().decode())
    return r["id"]

def export(file_id, pdf_out, mime="application/pdf"):
    H = _H()
    url = (f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
           f"?mimeType={urllib.parse.quote(mime)}")
    b = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=300).read()
    open(pdf_out, "wb").write(b); return pdf_out

def listdir(folder_id):
    H = _H()
    url = (f"https://www.googleapis.com/drive/v3/files?q='{folder_id}'+in+parents"
           "+and+trashed=false&fields=files(id,name,mimeType,size)"
           "&supportsAllDrives=true&includeItemsFromAllDrives=true&pageSize=200")
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=H), timeout=60).read().decode()).get("files", [])

def _slides_url(fid): return f"https://docs.google.com/presentation/d/{fid}/edit"
def _doc_url(fid):    return f"https://docs.google.com/document/d/{fid}/edit"

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "up-slides":
        fid = create(sys.argv[2], sys.argv[3], sys.argv[4], DECK, PPTX); print(_slides_url(fid)); print("ID", fid)
    elif cmd == "patch-slides":
        fid = patch(sys.argv[2], sys.argv[3], PPTX); print(_slides_url(fid))
    elif cmd == "up-doc":
        fid = create(sys.argv[2], sys.argv[3], sys.argv[4], DOC, "text/html"); print(_doc_url(fid)); print("ID", fid)
    elif cmd == "patch-doc":
        fid = patch(sys.argv[2], sys.argv[3], "text/html"); print(_doc_url(fid))
    elif cmd == "export":
        print(export(sys.argv[3], sys.argv[2]))
    elif cmd == "ls":
        for f in listdir(sys.argv[2]):
            print(f.get("mimeType","?")[-28:].ljust(28), (f.get("size") or "-").rjust(10), f["id"], f["name"])
    else:
        print(__doc__)
