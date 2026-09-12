import os
import json
import time
import mimetypes
import tempfile
import requests
import subprocess

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
READY_FOLDER_ID = os.environ["READY_FOLDER_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/youtube.upload",
]


def google_credentials():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def get_next_video(drive):
    response = drive.files().list(
        q=f"'{READY_FOLDER_ID}' in parents and trashed = false",
        orderBy="createdTime asc",
        pageSize=100,
        fields="files(id,name,mimeType,size,parents)",
    ).execute()
    for file in response.get("files", []):
        mime = file.get("mimeType", "")
        name = file.get("name", "").lower()
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")):
            return file
    return None


def download_video(drive, file_id, filename, expected_size=None):
    safe_name = os.path.basename(filename)
    temp_dir = tempfile.mkdtemp()
    path = os.path.join(temp_dir, safe_name)
    expected = int(expected_size) if expected_size not in (None, "") else None

    last_error = None
    for attempt in range(1, 4):
        try:
            request = drive.files().get_media(fileId=file_id)
            with open(path, "wb") as output:
                downloader = MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    status, done = downloader.next_chunk(num_retries=5)
                    if status:
                        print(f"Download progress: {int(status.progress() * 100)}%")

            actual = os.path.getsize(path)
            print(f"Downloaded bytes: {actual}")
            if actual <= 0:
                raise RuntimeError("Downloaded video is empty.")
            if expected is not None and actual != expected:
                raise RuntimeError(f"Drive download size mismatch: expected {expected} bytes, got {actual} bytes.")

            # Decode-check the container before doing any normalization/upload work.
            check = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-f", "null", "-"],
                capture_output=True,
                text=True,
            )
            if check.returncode != 0:
                raise RuntimeError("Downloaded video failed decode validation: " + check.stderr[-1000:])

            return path
        except Exception as error:
            last_error = error
            print(f"Drive download validation failed on attempt {attempt}/3: {error}")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            if attempt < 3:
                time.sleep(3 * attempt)

    raise RuntimeError(f"Could not obtain a complete valid video from Drive after 3 attempts: {last_error}")


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    value = result.stdout.strip()
    return float(value) if value else 0.0


def normalize_video(input_path):
    source_duration = probe_duration(input_path)
    print(f"Source duration detected: {source_duration:.2f} seconds")

    if source_duration < 2.0:
        raise RuntimeError(
            f"Source video duration is only {source_duration:.2f}s. Refusing to upload a broken/partial file."
        )

    output_path = os.path.join(os.path.dirname(input_path), "youtube_normalized.mp4")
    command = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0:
        print(process.stderr)
        raise RuntimeError("FFmpeg normalization failed.")

    normalized_duration = probe_duration(output_path)
    print(f"Normalized duration: {normalized_duration:.2f} seconds")
    if normalized_duration < 2.0:
        raise RuntimeError("Normalized video is still too short; upload stopped for safety.")
    if abs(normalized_duration - source_duration) > max(2.0, source_duration * 0.20):
        raise RuntimeError(
            f"Duration changed unexpectedly from {source_duration:.2f}s to {normalized_duration:.2f}s; upload stopped."
        )
    return output_path


def upload_to_gemini(video_path):
    file_size = os.path.getsize(video_path)
    if file_size > 2_000_000_000:
        raise Exception("Video is larger than Gemini file limit.")
    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
    start_url = "https://generativelanguage.googleapis.com/" + f"upload/v1beta/files?key={GEMINI_API_KEY}"
    metadata = {"file": {"display_name": os.path.basename(video_path)}}
    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json",
    }
    start = requests.post(start_url, headers=headers, json=metadata, timeout=60)
    start.raise_for_status()
    upload_url = start.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise Exception("Gemini did not return upload URL.")
    upload_headers = {
        "Content-Length": str(file_size),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    with open(video_path, "rb") as video_file:
        uploaded = requests.post(upload_url, headers=upload_headers, data=video_file, timeout=300)
    uploaded.raise_for_status()
    return uploaded.json().get("file", uploaded.json())


def wait_for_gemini_file(file_info):
    name = file_info.get("name")
    if not name:
        return file_info
    url = "https://generativelanguage.googleapis.com/" + f"v1beta/{name}?key={GEMINI_API_KEY}"
    for _ in range(30):
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        info = response.json()
        state = info.get("state")
        if isinstance(state, dict):
            state = state.get("name")
        print("Gemini status:", state)
        if state == "ACTIVE":
            return info
        if state == "FAILED":
            raise RuntimeError("Gemini video processing failed.")
        time.sleep(2)
    raise RuntimeError("Gemini processing timed out.")


def analyze_video(file_info):
    prompt = "Return JSON only with title, description, hashtags, tags, category_id for this YouTube Short. Keep title under 95 chars and use relevant metadata only."
    file_uri = file_info.get("uri")
    if not file_uri:
        raise RuntimeError("Gemini file URI missing.")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    payload = {"contents": [{"parts": [{"text": prompt}, {"fileData": {"mimeType": "video/mp4", "fileUri": file_uri}}]}]}
    response = requests.post(url, json=payload, timeout=180)
    response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def fallback_metadata(filename):
    name = os.path.splitext(os.path.basename(filename))[0].replace("_", " ").replace("-", " ")
    return {"title": name[:95] or "New Short", "description": "", "hashtags": ["Shorts"], "tags": ["shorts"], "category_id": "22"}


def prepare_metadata(metadata):
    title = str(metadata.get("title", "New Short")).strip()[:95]
    description = str(metadata.get("description", "")).strip()
    hashtags = metadata.get("hashtags", [])
    if isinstance(hashtags, list):
        cleaned = []
        for tag in hashtags[:5]:
            tag = str(tag).strip()
            if tag:
                if not tag.startswith("#"):
                    tag = "#" + tag.replace(" ", "")
                cleaned.append(tag)
        if cleaned:
            description += "\n\n" + " ".join(cleaned)
    tags = metadata.get("tags", []) if isinstance(metadata.get("tags", []), list) else []
    final_tags = []
    total = 0
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        if total + len(tag) > 450:
            break
        final_tags.append(tag)
        total += len(tag)
    allowed = {"1","2","10","15","17","19","20","22","23","24","25","26","27","28"}
    category_id = str(metadata.get("category_id", "22"))
    if category_id not in allowed:
        category_id = "22"
    return {"title": title, "description": description[:5000], "tags": final_tags, "category_id": category_id}


def upload_to_youtube(youtube, video_path, metadata):
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["category_id"],
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=False)
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            print(f"YouTube upload: {int(status.progress() * 100)}%")
    return response


def verify_youtube_public(youtube, video_id):
    for attempt in range(1, 7):
        response = youtube.videos().list(part="status,processingDetails", id=video_id).execute()
        items = response.get("items", [])
        if not items:
            raise RuntimeError("Uploaded YouTube video could not be retrieved for verification.")
        item = items[0]
        privacy = item.get("status", {}).get("privacyStatus")
        processing = item.get("processingDetails", {}).get("processingStatus")
        print(f"YouTube verification attempt {attempt}: privacy={privacy}, processing={processing}")
        if privacy == "public":
            return
        time.sleep(5)
    raise RuntimeError("YouTube did not keep the uploaded video public. The Google API project may be subject to YouTube's unverified-project private-upload restriction.")


def delete_from_drive(drive, file_id):
    print("YouTube upload confirmed public.")
    print("Deleting original video from Google Drive...")
    drive.files().delete(fileId=file_id).execute()
    print("Original video permanently deleted from Google Drive.")


def delete_gemini_file(file_info):
    try:
        url = "https://generativelanguage.googleapis.com/" + f"v1beta/{file_info['name']}?key={GEMINI_API_KEY}"
        requests.delete(url, timeout=30)
    except Exception as error:
        print("Could not delete Gemini temporary file:", error)


def main():
    print("Starting YouTube Shorts automation...")
    creds = google_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    video = get_next_video(drive)
    if not video:
        print("No videos found in READY folder.")
        return

    print("Selected video:", video["name"])
    original_path = download_video(drive, video["id"], video["name"], video.get("size"))
    print("Validating and normalizing video timing...")
    video_path = normalize_video(original_path)
    gemini_file = None

    try:
        print("Sending normalized video to Gemini...")
        gemini_file = upload_to_gemini(video_path)
        gemini_file = wait_for_gemini_file(gemini_file)
        print("Generating Shorts metadata...")
        metadata = analyze_video(gemini_file)
    except Exception as error:
        print("Gemini analysis failed:", error)
        print("Using fallback metadata.")
        metadata = fallback_metadata(video["name"])

    metadata = prepare_metadata(metadata)
    print("Generated title:", metadata["title"])
    print("Final verified duration before upload:", f"{probe_duration(video_path):.2f}s")
    print("Uploading Short to YouTube with requested privacyStatus=public...")

    youtube_video = upload_to_youtube(youtube, video_path, metadata)
    video_id = youtube_video["id"]
    print("YouTube upload API completed.")
    print("YouTube Video ID:", video_id)
    verify_youtube_public(youtube, video_id)
    print("YouTube upload successful.")
    print("YouTube privacy verified: public")

    delete_from_drive(drive, video["id"])

    if gemini_file:
        delete_gemini_file(gemini_file)

    for path in {original_path, video_path}:
        try:
            os.remove(path)
        except Exception:
            pass

    print("Automation completed successfully.")


if __name__ == "__main__":
    main()
