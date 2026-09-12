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


def download_video(drive, file_id, filename):
    safe_name = os.path.basename(filename)
    temp_dir = tempfile.mkdtemp()
    path = os.path.join(temp_dir, safe_name)
    request = drive.files().get_media(fileId=file_id)
    with open(path, "wb") as output:
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Download progress: {int(status.progress() * 100)}%")
    if os.path.getsize(path) <= 0:
        raise RuntimeError("Downloaded video is empty.")
    return path


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

    output_path = os.path.join(
        os.path.dirname(input_path),
        "youtube_normalized.mp4",
    )

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
        "-vsync", "cfr",
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
    start_url = (
        "https://generativelanguage.googleapis.com/"
        f"upload/v1beta/files?key={GEMINI_API_KEY}"
    )
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
        uploaded = requests.post(
            upload_url,
            headers=upload_headers,
            data=video_file,
            timeout=1800,
        )
    uploaded.raise_for_status()
    return uploaded.json()["file"]


def wait_for_gemini_file(file_info):
    file_name = file_info["name"]
    status_url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/{file_name}?key={GEMINI_API_KEY}"
    )
    for _ in range(60):
        response = requests.get(status_url, timeout=30)
        response.raise_for_status()
        data = response.json()
        state = data.get("state", "")
        print("Gemini file state:", state)
        if state == "ACTIVE":
            return data
        if state == "FAILED":
            raise Exception("Gemini failed to process video.")
        time.sleep(10)
    raise Exception("Gemini processing timed out.")


def clean_json(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def analyze_video(video_file):
    prompt = """
Watch this complete video carefully. It is intended as a YouTube Short.
Return ONLY valid JSON with title, description, tags, hashtags and category_id.
Use the video's actual language. Do not invent facts, people or brands.
Title max 95 characters. Use 8-15 tags and 3-5 hashtags. Include #shorts when appropriate.
Valid categories: 1,2,10,15,17,19,20,22,23,24,25,26,27,28.
Example shape:
{"title":"...","description":"...","tags":["..."],"hashtags":["#shorts"],"category_id":"22"}
"""
    endpoint = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.5-flash:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [
            {"file_data": {"mime_type": video_file["mimeType"], "file_uri": video_file["uri"]}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    response = requests.post(endpoint, json=payload, timeout=600)
    response.raise_for_status()
    result = response.json()
    text = result["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(clean_json(text))


def fallback_metadata(filename):
    title = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ").strip() or "New Short"
    return {
        "title": title[:95],
        "description": f"{title}\n\n#shorts",
        "tags": ["shorts", "youtube shorts"],
        "hashtags": ["#shorts"],
        "category_id": "22",
    }


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

    return {
        "title": title,
        "description": description[:5000],
        "tags": final_tags,
        "category_id": category_id,
    }


def upload_to_youtube(youtube, video_path, metadata):
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["category_id"],
        },
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"YouTube upload: {int(status.progress() * 100)}%")
    return response


def delete_from_drive(drive, file_id):
    print("YouTube upload confirmed.")
    print("Deleting original video from Google Drive...")
    drive.files().delete(fileId=file_id).execute()
    print("Original video permanently deleted from Google Drive.")


def delete_gemini_file(file_info):
    try:
        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/{file_info['name']}?key={GEMINI_API_KEY}"
        )
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
    original_path = download_video(drive, video["id"], video["name"])
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
    print("Uploading Short to YouTube...")

    youtube_video = upload_to_youtube(youtube, video_path, metadata)
    print("YouTube upload successful.")
    print("YouTube Video ID:", youtube_video["id"])

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
