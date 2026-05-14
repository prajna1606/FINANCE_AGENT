from dotenv import load_dotenv
from google import genai
import os
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client= genai.Client(api_key=api_key)
from datetime import date
from datetime import datetime
import pandas as pd
import numpy as np
import time
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.blocking import BlockingScheduler

dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

def sanitize(value):
    return str(value).strip().replace("\n", " ").replace("\r", " ")

def generate_email(row, prompt):
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt + "\n\nInvoice details:\n" +
                f"Client name: {sanitize(row['client_name'])}, "
                f"Invoice No: {sanitize(row['invoice_no'])}, "
                f"Amount: {sanitize(row['amount'])}, "
                f"Due Date: {sanitize(row['due_date'])}, "
                f"Days Due: {sanitize(row['days_due'])}"
            )
            text = response.text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except Exception as e:
            print(f"Attempt {attempt+1} failed:", e)
            time.sleep(10)

    return None

def send_email(to_email, subject, body):
    sender_email = os.getenv("EMAIL_USER")
    sender_password = os.getenv("EMAIL_PASS")

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {to_email}")
        return "sent"
    except Exception as e:
        print("Email failed:", e)
        return "failed"

def log_email(row, email, stage, send_status):
    os.makedirs("logs", exist_ok=True)
    name_parts= row["client_name"].split()
    if len(name_parts) >= 2:
        masked_name = name_parts[0][0]+ "***" + " " + name_parts[-1][0]+ "***"
    else:
        masked_name = row["client_name"]
    log_data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "invoice_no": row["invoice_no"],
        "client_name": masked_name,
        "stage": stage,
        "subject": email["subject"],
        "body": "[REDACTED]",
        "send_status": send_status
    }
    file_name = f"logs/{row['invoice_no']}.json"
    with open(file_name, "w") as file:
        json.dump(log_data, file, indent=4)
    print(f"Log saved for {row['invoice_no']}")

def validate_email(email, row):
    if not email:
        return False
    if "subject" not in email or "body" not in email:
        return False
    if not email["subject"] or not email["body"]:
        return False
    if row["invoice_no"] not in email["body"]:
        return False
    if "https://company.com/pay/" not in email["body"]:
        return False
    if str(row["amount"]) not in email["body"]:
        return False
    if str(row["days_due"]) not in email["body"]:
        return False
    if str(row["due_date"]) not in email["body"]:
        return False
    if row["client_name"].split()[0] not in email["body"]:
        return False
    return True

def run_agent():
    df = pd.read_csv("data/invoices.csv")
    df["due_date"] = pd.to_datetime(df["due_date"]).dt.date
    today = date.today()
    df["days_due"] = (pd.Timestamp.today().date() - df["due_date"]).apply(lambda x: x.days)

    conditions = [
        (df["days_due"] >= 0)  & (df["days_due"] <= 7),
        (df["days_due"] > 7)   & (df["days_due"] <= 14),
        (df["days_due"] > 14)  & (df["days_due"] <= 21),
        (df["days_due"] > 21)  & (df["days_due"] <= 30),
        (df["days_due"] > 30)
   ]
    stages = ["stage 1", "stage 2", "stage 3", "stage 4", "escalate"]
    df["stage"] = np.select(conditions, stages, default="unknown")

    stage1_prompt = """
    You are a professional finance agent.

    Generate a warm and friendly payment reminder email for an overdue invoice.
    Assume the client simply overlooked the payment.

    Rules:
   - Tone: polite, positive, non-threatening
   - Must include: client name, invoice number, amount due, due date, days overdue, payment link
   - Payment link format: https://company.com/pay/<invoice_no>
   - Return ONLY a JSON object with keys "subject" and "body". No markdown, no extra text.
   """

    stage2_prompt = """
    You are a professional finance agent.

    Generate a polite but firm payment reminder email for an invoice that is 8-14 days overdue.
    A previous reminder was already sent and ignored.

    Rules:
    - Tone: professional, firm, concerned but respectful
    - Must include: client name, invoice number, amount due, due date, days overdue, payment link
    - Ask the client to confirm an exact payment date
    - Payment link format: https://company.com/pay/<invoice_no>
    - Return ONLY a JSON object with keys "subject" and "body". No markdown, no extra text.
    """

    stage3_prompt = """
    You are a professional finance agent.

    Generate a formal and serious payment demand email for an invoice that is 15-21 days overdue.
    Multiple reminders have been sent with no response.

    Rules:
    - Tone: formal, assertive, serious — no pleasantries
    - Must include: client name, invoice number, amount due, due date, days overdue, payment link
    - Demand a response within 48 hours
    - Mention that continued non-payment may impact their credit terms or business relationship
    - Payment link format: https://company.com/pay/<invoice_no>
    - Return ONLY a JSON object with keys "subject" and "body". No markdown, no extra text.
    """

    stage4_prompt = """
    You are a professional finance agent.

    Generate a stern final notice email for an invoice that is 22-30 days overdue.
    This is the last communication before the account is referred to the legal and recovery team.

    Rules:
    - Tone: stern, urgent, direct — make consequences crystal clear
    - Must include: client name, invoice number, amount due, due date, days overdue, payment link
    - State explicitly this is the final notice before legal escalation
    - Ask client to pay immediately or call the finance team
    - Payment link format: https://company.com/pay/<invoice_no>
    - Return ONLY a JSON object with keys "subject" and "body". No markdown, no extra text.
    """

    for index, row in df.iterrows():

        if row["stage"] == "stage 1":
            email = generate_email(row, stage1_prompt)
            if email is None or not validate_email(email, row):
                print(f"Validation Failed for {row['invoice_no']}")
                continue
            if dry_run:
                status = "dry_run"
            else:
                status = send_email(row["contact_email"], email["subject"], email["body"])
            log_email(row, email, "stage 1", status)

        elif row["stage"] == "stage 2":
            email = generate_email(row, stage2_prompt)
            if email is None or not validate_email(email, row):
                print(f"Validation Failed for {row['invoice_no']}")
                continue
            if dry_run:
                status = "dry_run"
            else:
                status = send_email(row["contact_email"], email["subject"], email["body"])
            log_email(row, email, "stage 2", status)

        elif row["stage"] == "stage 3":
            email = generate_email(row, stage3_prompt)
            if email is None or not validate_email(email, row):
                print(f"Validation Failed for {row['invoice_no']}")
                continue
            if dry_run:
                status = "dry_run"
            else:
                status = send_email(row["contact_email"], email["subject"], email["body"])
            log_email(row, email, "stage 3", status)

        elif row["stage"] == "stage 4":
            email = generate_email(row, stage4_prompt)
            if email is None or not validate_email(email, row):
                print(f"Validation Failed for {row['invoice_no']}")
                continue
            if dry_run:
                status = "dry_run"
            else:
                status = send_email(row["contact_email"], email["subject"], email["body"])
            log_email(row, email, "stage 4", status)

        elif row["stage"] == "escalate":
            print(f"Escalate to legal team: {row['invoice_no']}")
            continue

        print(email)
        time.sleep(5)
run_agent()
scheduler= BlockingScheduler()
scheduler.add_job(run_agent, 'cron', hour=9, minute=0)
print("Agent started.")  
scheduler.start()  