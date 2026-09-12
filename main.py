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

LEGACY_ALREADY_UPLOADED = {
    "ankitaistudio14_AQNuk7Z_c_9AZ-4KvZ3oQSW3_1787745623433.mp4": "fyBYm80DVkI",
}


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


def is_video(file):
    mime = file.get("mimeType", "")
    name = file.get("name", "").lower()
    return mime.startswith("video/") or name.endswith(
        (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
    )


def mark_youtube_uploaded(drive, file_id, video_id):
    drive.files().update(
        fileId=file_id,
        body={
            "appProperties": {
                "youtube_uploaded": "true",
                "youtube_video_id": str(video_id),
            }
        },
        fields="id,appProperties",
    ).execute()
    print("Drive source marked as already uploaded to YouTube.")


def delete_stale_uploaded_source(drive, file):
    try:
        drive.files().delete(fileId=file["id"]).execute()
        print("Removed stale already-uploaded source from READY:", file["name"])
    except Exception as error:
        print("Could not remove stale already-uploaded source:", error)


def get_next_video(drive):
    response = drive.files().list(
        q=f"'{READY_FOLDER_ID}' in parents and trashed = false",
        orderBy="createdTime asc",
        pageSize=100,
        fields="files(id,name,mimeType,size,parents,appProperties)",
    ).execute()

    for file in response.get("files", []):
        if not is_video(file):
            continue

        props = file.get("appProperties") or {}
        if props.get("youtube_uploaded") == "true":
            print("Skipping file already marked uploaded to YouTube:", file["name"])
            delete_stale_uploaded_source(drive, file)
            continue

        legacy_video_id = LEGACY_ALREADY_UPLOADED.get(file.get("name"))
        if legacy_video_id:
            print("Skipping previously confirmed YouTube upload:", file["name"])
            try:
                mark_youtube_uploaded(drive, file["id"], legacy_video_id)
            finally:
                delete_stale_uploaded_source(drive, file)
            continue

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
                raise RuntimeError(
                    f"Drive download size mismatch: expected {expected} bytes, got {actual} bytes."
                )

            check = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-f", "null", "-"],
                capture_output=True,
                text=True,
            )
            if check.returncode != 0:
                raise RuntimeError(
                    "Downloaded video failed decode validation: " + check.stderr[-1000:]
                )
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

    raise RuntimeError(
        f"Could not obtain a complete valid video from Drive after 3 attempts: {last_error}"
    )


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


def validate_original_video(input_path):
    duration = probe_duration(input_path)
    print(f"Original video duration: {duration:.2f} seconds")
    if duration < 2.0:
        raise RuntimeError(
            f"Source video duration is only {duration:.2f}s. Refusing to upload a broken/partial file."
        )

    decode_check = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", input_path, "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    if decode_check.returncode != 0:
        raise RuntimeError(
            "Original video failed decode validation: " + decode_check.stderr[-1000:]
        )

    print("Original video validated. No re-encoding will be done, so duration is preserved.")
    return input_path


def upload_to_gemini(video_path):
    file_size = os.path.getsize(video_path)
    if file_size > 2_000_000_000:
        raise RuntimeError("Video is larger than Gemini file limit.")

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
        raise RuntimeError("Gemini did not return upload URL.")

    upload_headers = {
        "Content-Length": str(file_size),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    with open(video_path, "rb") as video_file:
        uploaded = requests.post(
            upload_url, headers=upload_headers, data=video_file, timeout=300
        )
    uploaded.raise_for_status()
    return uploaded.json().get("file", uploaded.json())


def wait_for_gemini_file(file_info):
    name = file_info.get("name")
    if not name:
        raise RuntimeError("Gemini file name missing.")
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/{name}?key={GEMINI_API_KEY}"
    )
    for _ in range(45):
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
        time.sleep(3)
    raise RuntimeError("Gemini processing timed out.")


def clean_json(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def validate_metadata(metadata):
    title = str(metadata.get("title", "")).strip()
    description = str(metadata.get("description", "")).strip()
    hashtags = metadata.get("hashtags", [])
    tags = metadata.get("tags", [])
    if not title or len(title) < 3:
        raise RuntimeError("Gemini returned an empty/invalid YouTube title.")
    if not isinstance(hashtags, list):
        hashtags = []
    if not isinstance(tags, list):
        tags = []
    return {
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "tags": tags,
        "category_id": str(metadata.get("category_id", "22")),
    }


def analyze_video(video_file):
    prompt = """
Watch the COMPLETE video carefully and create the final YouTube Shorts upload material.

Return ONLY valid JSON:
{
  "title": "natural high-quality YouTube Shorts title",
  "description": "2-5 concise natural lines describing what actually happens in the video",
  "hashtags": ["#Shorts", "#RelevantTag2", "#RelevantTag3"],
  "tags": ["shorts", "relevant keyword", "another keyword"],
  "category_id": "22"
}

Rules:
- Base everything ONLY on the actual video.
- Detect the spoken/written language. Use natural Hinglish for Hindi/Hinglish content; use English for English content.
- Title must be human, clean, interesting, and under 95 characters.
- Do NOT use the source filename, random IDs, usernames, timestamps, or file codes as the title.
- Do NOT mention AI or that metadata was generated.
- Description must not be blank.
- Use 3-5 relevant hashtags and 5-12 relevant tags.
- No fake claims or invented people/brands.
- JSON only. No markdown.
""".strip()

    file_uri = video_file.get("uri")
    mime_type = video_file.get("mimeType") or "video/mp4"
    if not file_uri:
        raise RuntimeError("Gemini file URI missing.")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"fileData": {"mimeType": mime_type, "fileUri": file_uri}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {
            "temperature": 0.55,
            "responseMimeType": "application/json",
        },
    }

    last_error = None
    for attempt in range(1, 6):
        try:
            print(f"Gemini metadata attempt {attempt}/5...")
            response = requests.post(url, json=payload, timeout=180)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(
                    f"Temporary Gemini error {response.status_code}: {response.text[:300]}"
                )
            response.raise_for_status()
            result = response.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return validate_metadata(json.loads(clean_json(text)))
        except Exception as error:
            last_error = error
            print("Gemini metadata attempt failed:", error)
            if attempt < 5:
                time.sleep(8 * attempt)

    raise RuntimeError(
        f"Could not generate YouTube title/description/hashtags after 5 attempts: {last_error}"
    )


def prepare_metadata(metadata):
    title = str(metadata["title"]).strip()[:95]
    description = str(metadata.get("description", "")).strip()

    clean_hashtags = []
    for tag in metadata.get("hashtags", [])[:5]:
        tag = str(tag).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag.replace(" ", "")
        clean_hashtags.append(tag)

    if clean_hashtags:
        description = (description + "\n\n" + " ".join(clean_hashtags)).strip()

    final_tags = []
    total = 0
    for tag in metadata.get("tags", []):
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

    if not title or not description:
        raise RuntimeError("Generated YouTube upload material is incomplete; upload stopped.")

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
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
    media = MediaFileUpload(
        video_path, mimetype=mime_type, chunksize=8 * 1024 * 1024, resumable=True
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            print(f"YouTube upload: {int(status.progress() * 100)}%")
    return response


def verify_youtube_upload_response(upload_response):
    privacy = upload_response.get("status", {}).get("privacyStatus")
    print("YouTube upload response privacy:", privacy)
    if privacy != "public":
        raise RuntimeError(
            "YouTube accepted the upload but did not return privacyStatus=public."
        )


def delete_from_drive(drive, file_id):
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
    original_path = None
    video_path = None
    gemini_file = None

    try:
        original_path = download_video(
            drive, video["id"], video["name"], video.get("size")
        )
        print("Validating original video without changing it...")
        video_path = validate_original_video(original_path)

        print("Sending original-duration video to Gemini...")
        gemini_file = upload_to_gemini(video_path)
        gemini_file = wait_for_gemini_file(gemini_file)

        print("Generating YouTube title, description, hashtags and tags...")
        metadata = prepare_metadata(analyze_video(gemini_file))
        print("Generated title:", metadata["title"])
        print("Generated description/hashtags ready.")
        print("Final upload duration:", f"{probe_duration(video_path):.2f}s")

        print("Uploading original video to YouTube with requested privacyStatus=public...")
        youtube_video = upload_to_youtube(youtube, video_path, metadata)
        video_id = youtube_video["id"]
        print("YouTube upload API completed.")
        print("YouTube Video ID:", video_id)

        mark_youtube_uploaded(drive, video["id"], video_id)

        verify_youtube_upload_response(youtube_video)
        print("YouTube upload successful.")
        print("YouTube privacy verified from upload response: public")

        try:
            delete_from_drive(drive, video["id"])
        except Exception as error:
            print(
                "Drive delete warning: source could not be deleted, but it is marked "
                "youtube_uploaded=true so it will NOT be uploaded again:",
                error,
            )

        print("Automation completed successfully.")
    finally:
        if gemini_file:
            delete_gemini_file(gemini_file)
        for path in {original_path, video_path}:
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
