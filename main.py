import os
import json
import time
import mimetypes
import tempfile
import requests

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

        if mime.startswith("video/") or name.endswith(
            (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
        ):
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
                print(
                    f"Download progress: "
                    f"{int(status.progress() * 100)}%"
                )

    return path


def upload_to_gemini(video_path):
    file_size = os.path.getsize(video_path)

    if file_size > 2_000_000_000:
        raise Exception(
            "Video is larger than Gemini file limit."
        )

    mime_type = (
        mimetypes.guess_type(video_path)[0]
        or "video/mp4"
    )

    start_url = (
        "https://generativelanguage.googleapis.com/"
        f"upload/v1beta/files?key={GEMINI_API_KEY}"
    )

    metadata = {
        "file": {
            "display_name": os.path.basename(video_path)
        }
    }

    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json",
    }

    start = requests.post(
        start_url,
        headers=headers,
        json=metadata,
        timeout=60,
    )

    start.raise_for_status()

    upload_url = start.headers.get(
        "X-Goog-Upload-URL"
    )

    if not upload_url:
        raise Exception(
            "Gemini did not return upload URL."
        )

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

        response = requests.get(
            status_url,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        state = data.get("state", "")

        print("Gemini file state:", state)

        if state == "ACTIVE":
            return data

        if state == "FAILED":
            raise Exception(
                "Gemini failed to process video."
            )

        time.sleep(10)

    raise Exception(
        "Gemini processing timed out."
    )


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
Watch this complete video carefully.

This video is intended to be uploaded as a YouTube Short.

Create professional YouTube Shorts uploading material based ONLY
on what actually happens in the video.

Detect the main language of the video.

If the video uses Hindi/Hinglish, use natural Hinglish.
If it uses English, use English.

Return ONLY valid JSON:

{
  "title": "short engaging title",
  "description": "short professional description",
  "tags": ["tag1", "tag2"],
  "hashtags": ["#shorts", "#tag2", "#tag3"],
  "category_id": "22"
}

RULES:

- Title must be maximum 95 characters.
- Make the title suitable for YouTube Shorts.
- Keep the description short and natural.
- Create 8 to 15 relevant tags.
- Create 3 to 5 strong hashtags.
- Include #shorts when appropriate.
- Do not use fake clickbait.
- Do not invent facts.
- Do not invent people or brands.
- Do not mention AI.
- Do not include markdown.
- Return JSON only.

Choose the closest YouTube category:

1 = Film & Animation
2 = Autos & Vehicles
10 = Music
15 = Pets & Animals
17 = Sports
19 = Travel & Events
20 = Gaming
22 = People & Blogs
23 = Comedy
24 = Entertainment
25 = News & Politics
26 = Howto & Style
27 = Education
28 = Science & Technology
"""

    endpoint = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.5-flash:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "file_data": {
                            "mime_type":
                            video_file["mimeType"],

                            "file_uri":
                            video_file["uri"],
                        }
                    },
                    {
                        "text": prompt
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType":
            "application/json",
        },
    }

    response = requests.post(
        endpoint,
        json=payload,
        timeout=600,
    )

    response.raise_for_status()

    result = response.json()

    text = (
        result["candidates"][0]
        ["content"]["parts"][0]["text"]
    )

    return json.loads(
        clean_json(text)
    )


def fallback_metadata(filename):

    title = os.path.splitext(filename)[0]

    title = (
        title
        .replace("_", " ")
        .replace("-", " ")
        .strip()
    )

    if not title:
        title = "New Short"

    return {
        "title": title[:95],

        "description":
        f"{title}\n\n#shorts",

        "tags": [
            "shorts",
            "youtube shorts"
        ],

        "hashtags": [
            "#shorts"
        ],

        "category_id": "22",
    }


def prepare_metadata(metadata):

    title = str(
        metadata.get(
            "title",
            "New Short"
        )
    ).strip()[:95]

    description = str(
        metadata.get(
            "description",
            ""
        )
    ).strip()

    hashtags = metadata.get(
        "hashtags",
        []
    )

    if isinstance(hashtags, list):

        clean_hashtags = []

        for tag in hashtags[:5]:

            tag = str(tag).strip()

            if tag:

                if not tag.startswith("#"):

                    tag = (
                        "#"
                        + tag.replace(" ", "")
                    )

                clean_hashtags.append(tag)

        if clean_hashtags:

            description += (
                "\n\n"
                + " ".join(clean_hashtags)
            )

    tags = metadata.get(
        "tags",
        []
    )

    if not isinstance(tags, list):
        tags = []

    final_tags = []

    total_length = 0

    for tag in tags:

        tag = str(tag).strip()

        if not tag:
            continue

        new_length = (
            total_length
            + len(tag)
        )

        if new_length > 450:
            break

        final_tags.append(tag)

        total_length = new_length

    allowed_categories = {
        "1",
        "2",
        "10",
        "15",
        "17",
        "19",
        "20",
        "22",
        "23",
        "24",
        "25",
        "26",
        "27",
        "28",
    }

    category_id = str(
        metadata.get(
            "category_id",
            "22"
        )
    )

    if category_id not in allowed_categories:
        category_id = "22"

    return {
        "title": title,
        "description": description[:5000],
        "tags": final_tags,
        "category_id": category_id,
    }


def upload_to_youtube(
    youtube,
    video_path,
    metadata,
):

    body = {
        "snippet": {
            "title":
            metadata["title"],

            "description":
            metadata["description"],

            "tags":
            metadata["tags"],

            "categoryId":
            metadata["category_id"],
        },

        "status": {
            "privacyStatus": "private"
        },
    }

    media = MediaFileUpload(
        video_path,
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

        status, response = (
            request.next_chunk()
        )

        if status:

            print(
                "YouTube upload: "
                f"{int(status.progress() * 100)}%"
            )

    return response


def delete_from_drive(
    drive,
    file_id
):

    print(
        "YouTube upload confirmed."
    )

    print(
        "Deleting original video "
        "from Google Drive..."
    )

    drive.files().delete(
        fileId=file_id
    ).execute()

    print(
        "Original video permanently "
        "deleted from Google Drive."
    )


def delete_gemini_file(file_info):

    try:

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/{file_info['name']}"
            f"?key={GEMINI_API_KEY}"
        )

        requests.delete(
            url,
            timeout=30
        )

    except Exception as error:

        print(
            "Could not delete "
            "Gemini temporary file:",
            error
        )


def main():

    print(
        "Starting YouTube Shorts automation..."
    )

    creds = google_credentials()

    drive = build(
        "drive",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )

    youtube = build(
        "youtube",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )

    video = get_next_video(drive)

    if not video:

        print(
            "No videos found "
            "in READY folder."
        )

        return

    print(
        "Selected video:",
        video["name"]
    )

    video_path = download_video(
        drive,
        video["id"],
        video["name"],
    )

    gemini_file = None

    try:

        print(
            "Sending video to Gemini..."
        )

        gemini_file = upload_to_gemini(
            video_path
        )

        gemini_file = wait_for_gemini_file(
            gemini_file
        )

        print(
            "Generating Shorts metadata..."
        )

        metadata = analyze_video(
            gemini_file
        )

    except Exception as error:

        print(
            "Gemini analysis failed:",
            error
        )

        print(
            "Using fallback metadata."
        )

        metadata = fallback_metadata(
            video["name"]
        )

    metadata = prepare_metadata(
        metadata
    )

    print(
        "Generated title:",
        metadata["title"]
    )

    print(
        "Uploading Short to YouTube..."
    )

    youtube_video = upload_to_youtube(
        youtube,
        video_path,
        metadata,
    )

    print(
        "YouTube upload successful."
    )

    print(
        "YouTube Video ID:",
        youtube_video["id"]
    )

    # IMPORTANT:
    # The Drive video is deleted ONLY
    # after YouTube confirms successful upload.

    delete_from_drive(
        drive,
        video["id"]
    )

    if gemini_file:

        delete_gemini_file(
            gemini_file
        )

    try:

        os.remove(video_path)

    except Exception:
        pass

    print(
        "Automation completed successfully."
    )


if __name__ == "__main__":
    main()
