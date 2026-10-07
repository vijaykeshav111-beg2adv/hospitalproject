import os
import base64
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/gmail.send"
]

CREDENTIALS_FILE = "credentials/credentials.json"
TOKEN_FILE = "credentials/token.json"


def get_gmail_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES
        )

    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES
            )

            creds = flow.run_local_server(
                port=0,
                open_browser=True
            )

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build(
        "gmail",
        "v1",
        credentials=creds
    )


def send_email(service, sender, recipient):
    message = MIMEText(
        """Hello,

This is a test email from Vijay Vargiya Group of Hospitals.

Gmail OAuth integration is working successfully.

Regards,
Vijay Vargiya Group of Hospitals
"""
    )

    message["to"] = recipient
    message["from"] = sender
    message["subject"] = (
        "Vijay Vargiya Hospital - Gmail Test"
    )

    raw_message = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    result = service.users().messages().send(
        userId="me",
        body={
            "raw": raw_message
        }
    ).execute()

    return result


if __name__ == "__main__":

    print("=" * 60)
    print("VIJAY VARGIYA HOSPITAL - GMAIL TEST")
    print("=" * 60)

    print("\nStarting Gmail OAuth...")

    service = get_gmail_service()

    sender = input(
        "\nHospital Gmail address: "
    ).strip()

    recipient = input(
        "Test recipient email: "
    ).strip()

    result = send_email(
        service,
        sender,
        recipient
    )

    print("\n" + "=" * 60)
    print("EMAIL SENT SUCCESSFULLY")
    print("=" * 60)
    print("Message ID:", result.get("id"))