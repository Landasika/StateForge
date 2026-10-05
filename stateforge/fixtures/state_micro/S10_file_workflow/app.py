"""S10:File-backed workflow — upload/approve/process/delete。"""
import os

UPLOAD_DIR = "/tmp/uploads"


def upload(filename, content):
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "w") as f:
        f.write(content)
    return {"filename": filename, "status": "uploaded"}


def approve(filename):
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        return "not found"
    marker = path + ".approved"
    with open(marker, "w") as f:
        f.write("approved")
    return "approved"


def process(filename):
    path = os.path.join(UPLOAD_DIR, filename)
    marker = path + ".approved"
    if not os.path.exists(marker):
        return "not approved"
    with open(path, "r") as f:
        data = f.read()
    return f"processed: {data}"


def delete_file(filename):
    path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
    marker = path + ".approved"
    if os.path.exists(marker):
        os.remove(marker)
    return "deleted"
