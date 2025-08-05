import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Get credentials from environment variable
creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
creds_dict = json.loads(creds_json)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

drive_service = build('drive', 'v3', credentials=creds)

# Your file and sheet info
excel_file = "scrcmd_database.xlsx"  # or your actual file name
sheet_name = "new scrcmd database"       # Name you want for the Google Sheet

# If you want to overwrite an existing file, find it by name (or by ID if you have it)
results = drive_service.files().list(
    q=f"name='{sheet_name}' and mimeType='application/vnd.google-apps.spreadsheet'",
    fields="files(id, name)").execute()
files = results.get('files', [])

media = MediaFileUpload(excel_file, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', resumable=True)

if files:
    # Update (replace) the file
    file_id = files[0]['id']
    updated_file = drive_service.files().update(
        fileId=file_id,
        media_body=media,
        fields='id'
    ).execute()
    print(f"Updated Google Sheet: https://docs.google.com/spreadsheets/d/{updated_file['id']}")
else:
    # Upload new, convert to Google Sheets format
    file_metadata = {
        'name': sheet_name,
        'mimeType': 'application/vnd.google-apps.spreadsheet'
    }
    new_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()
    print(f"Uploaded new Google Sheet: https://docs.google.com/spreadsheets/d/{new_file['id']}")
