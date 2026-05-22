import time
import os
import json
import smtplib
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

load_dotenv()

# Configurations
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
MIN_EARTHQUAKE_MAGNITUDE = 5.5     # Only alert for significant seismic activity
TEMP_ALERT_THRESHOLD_HIGH = 40.0   # Extreme heat alert Celsius (e.g., New Delhi summer)
TEMP_ALERT_THRESHOLD_LOW = 5.0     # Extreme cold alert Celsius
LATITUDE = 28.5355                 # Latitude for New Delhi (Vasant Kunj area)
LONGITUDE = 77.1558                # Longitude for New Delhi
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
HTTP_HEADERS = {
    "User-Agent": "HazardAlert/1.0 (+local monitoring script)"
}

STATE_FILE = os.getenv("HAZARD_ALERT_STATE_FILE", ".hazard_alert_state.json")
alert_state = {
    "sent_alert_keys": [],
    "active_weather_alert": "",
}


def load_alert_state():
    """Load persisted alert state so duplicate emails are avoided after restarts."""
    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved_state = json.load(file)
            alert_state["sent_alert_keys"] = saved_state.get("sent_alert_keys", [])
            alert_state["active_weather_alert"] = saved_state.get("active_weather_alert", "")
    except Exception as e:
        print(f"Warning: could not load alert state: {e}", flush=True)


def save_alert_state():
    """Persist alert state after sending a new alert."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(alert_state, file, indent=2)
    except Exception as e:
        print(f"Warning: could not save alert state: {e}", flush=True)


def has_sent_alert(alert_key):
    return alert_key in set(alert_state["sent_alert_keys"])


def mark_alert_sent(alert_key):
    if not has_sent_alert(alert_key):
        alert_state["sent_alert_keys"].append(alert_key)
        save_alert_state()


def parse_csv_env(value):
    """Parse comma-separated values from .env."""
    cleaned_value = (value or "").split("#", 1)[0]
    return [item.strip() for item in cleaned_value.split(",") if item.strip()]


def get_negative_news_keywords():
    default_keywords = (
        "accident,attack,blast,bomb,collapse,crime,curfew,danger,death,"
        "disease,epidemic,evacuation,explosion,fire,flood,heavy rain,"
        "kidnap,murder,outbreak,panic,protest,riot,shooting,stampede,"
        "terror,threat,toxic,violence,waterlogging"
    )
    return [keyword.lower() for keyword in parse_csv_env(os.getenv("NEWS_NEGATIVE_KEYWORDS", default_keywords))]


def get_news_rss_urls():
    configured_urls = parse_csv_env(os.getenv("NEWS_RSS_URLS"))
    if configured_urls:
        return configured_urls

    google_query = quote_plus(
        '("New Delhi" OR Delhi OR "Delhi NCR" OR Noida OR Gurugram) '
        '(fire OR blast OR explosion OR accident OR attack OR violence OR riot OR flood '
        'OR waterlogging OR collapse OR outbreak OR disease OR protest OR threat OR toxic)'
    )
    return [
        "https://www.hindustantimes.com/feeds/rss/cities/delhi-news/rssfeed.xml",
        "https://indianexpress.com/section/cities/delhi/feed/",
        f"https://news.google.com/rss/search?q={google_query}&hl=en-IN&gl=IN&ceid=IN:en",
    ]


def get_email_config():
    """Read email settings from .env."""
    smtp_user = os.getenv("EMAIL_USERNAME", "")
    sender_emails = parse_csv_env(os.getenv("EMAIL_FROM") or smtp_user)

    return {
        "enabled": os.getenv("EMAIL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        "smtp_host": os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("EMAIL_SMTP_PORT", "587")),
        "smtp_user": smtp_user,
        "smtp_pass": os.getenv("EMAIL_PASSWORD", ""),
        "sender_email": sender_emails[0] if sender_emails else smtp_user,
        "sender_name": os.getenv("EMAIL_SENDER_NAME", "Hazard Alert"),
        "recipients": parse_csv_env(os.getenv("EMAIL_TO")),
        "backup_smtp_host": os.getenv("BACKUP_EMAIL_SMTP_HOST", "smtp.gmail.com"),
        "backup_smtp_port": int(os.getenv("BACKUP_EMAIL_SMTP_PORT", "587")),
        "backup_user": os.getenv("BACKUP_EMAIL_USERNAME", ""),
        "backup_pass": os.getenv("BACKUP_EMAIL_PASSWORD", ""),
    }


def send_via_smtp(config, subject, text_body, smtp_user, smtp_pass, smtp_host, smtp_port):
    """Send a plain-text alert email with one SMTP account."""
    sender_email = config["sender_email"] if smtp_user == config["smtp_user"] else smtp_user

    msg = MIMEMultipart("alternative")
    msg["From"] = f'{config["sender_name"]} <{sender_email}>'
    msg["To"] = ", ".join(config["recipients"])
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(smtp_user, smtp_pass)
        server.sendmail(sender_email, config["recipients"], msg.as_string())


def send_email_alert(subject, text_body):
    """Send a plain-text alert email using EMAIL_* settings from .env."""
    config = get_email_config()

    if not config["enabled"]:
        print("Email not sent: EMAIL_ENABLED is disabled", flush=True)
        return False

    required = [
        config["smtp_host"],
        config["smtp_user"],
        config["smtp_pass"],
        config["sender_email"],
        config["recipients"],
    ]

    if not all(required):
        print("Email not sent: configure EMAIL_USERNAME, EMAIL_PASSWORD, and EMAIL_TO in .env", flush=True)
        return False

    try:
        send_via_smtp(
            config,
            subject,
            text_body,
            config["smtp_user"],
            config["smtp_pass"],
            config["smtp_host"],
            config["smtp_port"],
        )
        print(f"Email alert sent to {', '.join(config['recipients'])}", flush=True)
        return True
    except Exception as e:
        print(f"Primary email failed: {e}", flush=True)

    if not all([config["backup_user"], config["backup_pass"]]):
        print("Backup email not configured", flush=True)
        return False

    try:
        send_via_smtp(
            config,
            subject,
            text_body,
            config["backup_user"],
            config["backup_pass"],
            config["backup_smtp_host"],
            config["backup_smtp_port"],
        )
        print(f"Backup email alert sent to {', '.join(config['recipients'])}", flush=True)
        return True
    except Exception as backup_error:
        print(f"Backup email failed: {backup_error}", flush=True)
        return False

def alert(subject, body):
    """Print an alert and send it by email."""
    print(body, flush=True)
    return send_email_alert(subject, body)


def check_who_outbreaks():
    """Checks the WHO Disease Outbreak News API for new disease threats."""
    who_url = "https://www.who.int/api/hubs/diseaseoutbreaknews"
    params = {
        "$orderby": "PublicationDateAndTime desc",
        "$top": 1,
    }

    try:
        response = requests.get(who_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        items = data.get("value", [])
        if not items:
            return

        item = items[0]
        title = item.get("Title", "Untitled outbreak report")
        path = item.get("ItemDefaultUrl", "")
        link = f"https://www.who.int{path}" if path.startswith("/") else path
        alert_key = f"who:{link}"

        # Check if this is a newly published outbreak report
        if link and not has_sent_alert(alert_key):
            body = (
                f"[BIO-ALERT] WHO DISEASE OUTBREAK: {title}\n"
                f"Read full report: {link}"
            )
            if alert(f"BIO-ALERT: {title}", body):
                mark_alert_sent(alert_key)
    except Exception as e:
        print(f"Error checking WHO Feed: {e}", flush=True)


def check_temperature_anomalies():
    """Queries Open-Meteo API for real-time local temperature spikes or drops."""
    weather_url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m",
        "forecast_days": 1,
    }

    try:
        response = requests.get(weather_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        current_temp = data.get("current", {}).get("temperature_2m")

        if current_temp is not None:
            if current_temp >= TEMP_ALERT_THRESHOLD_HIGH:
                body = f"[WEATHER ALERT] Extreme Heat! Current Temp: {current_temp} C"
                if alert_state["active_weather_alert"] != "heat":
                    if alert("WEATHER ALERT: Extreme Heat", body):
                        alert_state["active_weather_alert"] = "heat"
                        save_alert_state()
                else:
                    print(f"[Weather Check] Extreme heat continues: {current_temp} C", flush=True)
            elif current_temp <= TEMP_ALERT_THRESHOLD_LOW:
                body = f"[WEATHER ALERT] Extreme Cold! Current Temp: {current_temp} C"
                if alert_state["active_weather_alert"] != "cold":
                    if alert("WEATHER ALERT: Extreme Cold", body):
                        alert_state["active_weather_alert"] = "cold"
                        save_alert_state()
                else:
                    print(f"[Weather Check] Extreme cold continues: {current_temp} C", flush=True)
            else:
                if alert_state["active_weather_alert"]:
                    alert_state["active_weather_alert"] = ""
                    save_alert_state()
                # Optional telemetry log
                print(f"[Weather Check] Local Temp is normal: {current_temp} C", flush=True)
    except Exception as e:
        print(f"Error checking weather: {e}", flush=True)


def check_earthquakes():
    """Fetches real-time earthquake data from USGS."""
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        for feature in data.get("features", []):
            earthquake_id = feature.get("id")
            properties = feature["properties"]
            mag = properties["mag"]
            place = properties["place"]
            alert_key = f"earthquake:{earthquake_id}"

            if mag and mag >= MIN_EARTHQUAKE_MAGNITUDE and not has_sent_alert(alert_key):
                body = f"[ALERT] EARTHQUAKE DETECTED! Mag: {mag} - Location: {place}"
                if alert(f"EARTHQUAKE ALERT: M{mag} - {place}", body):
                    mark_alert_sent(alert_key)
    except Exception as e:
        print(f"Error fetching USGS data: {e}", flush=True)


def get_xml_text(parent, tag_name):
    """Read text from RSS/Atom XML tags with or without namespaces."""
    if parent is None:
        return ""

    child = parent.find(tag_name)
    if child is not None and child.text:
        return child.text.strip()

    for element in parent:
        if element.tag.endswith(tag_name) and element.text:
            return element.text.strip()

    return ""


def get_feed_items(root):
    """Return RSS items or Atom entries from a parsed feed."""
    items = root.findall(".//item")
    if items:
        return items

    return [element for element in root.iter() if element.tag.endswith("entry")]


def get_item_link(item):
    """Return the best link value from an RSS item or Atom entry."""
    link = get_xml_text(item, "link")
    if link:
        return link

    for element in item:
        if element.tag.endswith("link") and element.attrib.get("href"):
            return element.attrib["href"].strip()

    return ""


def is_negative_delhi_news(title, description, keywords):
    text = f"{title} {description}".lower()
    return any(keyword in text for keyword in keywords)


def check_negative_news():
    """Checks Delhi/NCR RSS feeds for unusual negative news and sends one-time alerts."""
    if not NEWS_ENABLED:
        print("[News Check] News alerts disabled", flush=True)
        return

    keywords = get_negative_news_keywords()
    feed_urls = get_news_rss_urls()

    for feed_url in feed_urls:
        try:
            response = requests.get(feed_url, headers=HTTP_HEADERS, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)

            for item in get_feed_items(root)[:10]:
                title = unescape(get_xml_text(item, "title"))
                description = unescape(get_xml_text(item, "description") or get_xml_text(item, "summary"))
                link = get_item_link(item)

                if not title or not link:
                    continue

                alert_key = f"news:{link}"
                if has_sent_alert(alert_key):
                    continue

                if not is_negative_delhi_news(title, description, keywords):
                    continue

                body = (
                    f"[NEWS ALERT] Unusual negative news around New Delhi/NCR\n"
                    f"Headline: {title}\n"
                    f"Source feed: {feed_url}\n"
                    f"Read more: {link}"
                )
                if alert(f"NEWS ALERT: {title}", body):
                    mark_alert_sent(alert_key)
        except Exception as e:
            print(f"Error checking news feed {feed_url}: {e}", flush=True)


def main():
    load_alert_state()
    print(f"Starting Hazard Monitoring Engine at {datetime.now()}...", flush=True)
    while True:
        check_who_outbreaks()
        check_temperature_anomalies()
        check_earthquakes()
        check_negative_news()

        print(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds...\n", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
