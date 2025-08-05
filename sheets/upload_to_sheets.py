import os
import json
import gspread
from google.oauth2.service_account import Credentials

# Load service account credentials from env variable
creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
creds_dict = json.loads(creds_json)

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(credentials)

# Open by URL or sheet name
sh = gc.open_by_url("https://docs.google.com/spreadsheets/d/1WE6aCJeVbIMDfWYPykQEqLyBAZCDK8YlYFBD6hChiVA/")  # or gc.open("Your Sheet Name")

# Select worksheet
worksheet = sh.worksheet("Sheet1")  # Or whatever your sheet/tab is named

# Read your CSV or other data
import pandas as pd
df = pd.read_csv("scrcmd_dp.csv")  # adjust to your generated file

# Clear old data
worksheet.clear()

# Update with new data (as values, including header row)
worksheet.update([df.columns.values.tolist()] + df.values.tolist())
