import json
import logging
import os
import re
import signal
import sys
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup
from aksharamukha import transliterate

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline_runner.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

STATE_FILE = "crawler_state.json"
CONFIG_FILE = "parameters.yml"
SHUTDOWN_REQUESTED = False


def cm_to_pt(cm_value: float) -> float:
    """Convert centimetres to points for Google Docs style values."""
    return float(cm_value) * 28.3464567


def handle_shutdown(signum, frame):
    global SHUTDOWN_REQUESTED
    logging.info("Shutdown signal received. Exiting safely after the current item...")
    SHUTDOWN_REQUESTED = True


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


class ConfigManager:
    DEFAULTS = {
        "wikisource_url": "https://sa.wikisource.org/wiki/%E0%A4%A6%E0%A5%87%E0%A4%B5%E0%A5%80%E0%A4%AD%E0%A4%BE%E0%A4%97%E0%A4%B5%E0%A4%A4%E0%A4%AA%E0%A5%81%E0%A4%B0%E0%A4%BE%E0%A4%A3%E0%A4%AE%E0%A5%8D",
        "output_doc_name": None,
        "document_settings": {
            "font_size": 12,
            "include_referred_url": True,
            "enable_columns": True,
            "column_spacing_cm": 0.5,
            "line_between_columns": True,
            "margins": {
                "margin_left_cm": 1.5,
                "margin_right_cm": 1.5,
                "margin_top_cm": 1.5,
                "margin_bottom_cm": 1.5,
            },
        },
        "transliterate_params": {
            "src": "Devanagari",
            "tgt": "Kannada",
            "nativize": True,
            "pre_options": [],
            "post_options": ["Dot2Dandas", "KannadaNakaraPollu", "PreserveSource"],
        },
        "volume_groups": [],
        "crawler_settings": {
            "request_delay_seconds": 3.0,
            "batch_size": 10,
            "batch_cooldown_seconds": 30.0,
            "max_pages": None,
        },
    }

    @classmethod
    def load_config(cls, path: str = CONFIG_FILE) -> Dict[str, Any]:
        config = cls.DEFAULTS.copy()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_cfg = yaml.safe_load(f) or {}

                config["wikisource_url"] = user_cfg.get("wikisource_url", cls.DEFAULTS["wikisource_url"])
                config["output_doc_name"] = user_cfg.get("output_doc_name", cls.DEFAULTS["output_doc_name"])

                # Document Settings
                d_settings = user_cfg.get("document_settings", {})
                user_margins = d_settings.get("margins", {})
                config["document_settings"] = {
                    "font_size": d_settings.get("font_size", 12),
                    "include_referred_url": d_settings.get("include_referred_url", True),
                    "enable_columns": d_settings.get("enable_columns", True),
                    "column_spacing_cm": d_settings.get("column_spacing_cm", 0.5),
                    "line_between_columns": d_settings.get("line_between_columns", True),
                    "margins": {
                        "margin_left_cm": user_margins.get("margin_left_cm", 1.5),
                        "margin_right_cm": user_margins.get("margin_right_cm", 1.5),
                        "margin_top_cm": user_margins.get("margin_top_cm", 1.5),
                        "margin_bottom_cm": user_margins.get("margin_bottom_cm", 1.5),
                    },
                }

                # Transliteration Params
                t_params = user_cfg.get("transliterate_params", {})
                config["transliterate_params"] = {
                    "src": t_params.get("src", "Devanagari"),
                    "tgt": t_params.get("tgt", "Kannada"),
                    "nativize": t_params.get("nativize", True),
                    "pre_options": t_params.get("pre_options", []),
                    "post_options": t_params.get(
                        "post_options",
                        ["Dot2Dandas", "KannadaNakaraPollu", "PreserveSource"],
                    ),
                }

                config["volume_groups"] = user_cfg.get("volume_groups", [])

                # Crawler Settings
                c_settings = user_cfg.get("crawler_settings", {})
                config["crawler_settings"] = {
                    "request_delay_seconds": c_settings.get("request_delay_seconds", 3.0),
                    "batch_size": c_settings.get("batch_size", 10),
                    "batch_cooldown_seconds": c_settings.get("batch_cooldown_seconds", 30.0),
                    "max_pages": c_settings.get("max_pages", None),
                }
                logging.info(f"Loaded configuration from '{path}'.")
            except Exception as e:
                logging.warning(f"Error loading {path}, falling back to defaults: {e}")
        return config

class StateManager:
    """Maintains active document mappings per volume and processed pages."""

    @staticmethod
    def load_state() -> Dict[str, Any]:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logging.warning(f"Could not read state file: {e}")
        return {"volume_docs": {}, "processed_pages": []}

    @staticmethod
    def save_state(volume_docs: Dict[str, str], processed_pages: List[str]):
        data = {
            "volume_docs": volume_docs,
            "processed_pages": list(dict.fromkeys(processed_pages)),
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


class LocalAksharamukhaEngine:
    """Local transliteration processor."""

    def __init__(self, params: Dict[str, Any]):
        self.src = params.get("src", "Devanagari")
        self.tgt = params.get("tgt", "Kannada")
        self.nativize = params.get("nativize", True)
        self.pre_options = params.get("pre_options", [])
        self.post_options = params.get("post_options", [])

    def process(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        result = transliterate.process(
            src=self.src,
            tgt=self.tgt,
            txt=text,
            nativize=self.nativize,
            pre_options=self.pre_options,
            post_options=self.post_options,
        )
        return result if isinstance(result, str) else str(result or "")


class GoogleDocsManager:
    """Creates documents, applies multi-column and margin layouts, and appends text."""

    def __init__(self, credentials_path: str = "credentials.json", token_path: str = "token.json"):
        creds = None
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as token:
                token.write(creds.to_json())

        self.service = build("docs", "v1", credentials=creds, static_discovery=False)
        self.drive_service = build("drive", "v3", credentials=creds, static_discovery=False)

    def create_folder(self, name: str) -> str:
        """Creates a Google Drive folder and returns its ID."""
        folder = self.drive_service.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        folder_id = folder["id"]
        logging.info(f"Created Google Drive folder: '{name}' (ID: {folder_id})")
        return folder_id

    def create_document(
        self, title: str, doc_settings: Dict[str, Any], parent_folder_id: str
    ) -> str:
        """Creates a document and applies configured margins and column properties."""
        doc = self.service.documents().create(body={"title": title}).execute()
        doc_id = doc.get("documentId")
        logging.info(f"Created Document: '{title}' (ID: {doc_id})")

        parents = self.drive_service.files().get(
            fileId=doc_id, fields="parents"
        ).execute().get("parents", [])
        move_args = {
            "fileId": doc_id,
            "addParents": parent_folder_id,
            "fields": "id,parents",
        }
        if parents:
            move_args["removeParents"] = ",".join(parents)
        self.drive_service.files().update(**move_args).execute()

        self.apply_document_layout(doc_id, doc_settings)
        return doc_id

    def apply_document_layout(self, doc_id: str, doc_settings: Dict[str, Any]):
        """Applies margins and multi-column formatting to the document."""
        margins = doc_settings.get("margins", {})
        margin_left = cm_to_pt(margins.get("margin_left_cm", 1.5))
        margin_right = cm_to_pt(margins.get("margin_right_cm", 1.5))
        margin_top = cm_to_pt(margins.get("margin_top_cm", 1.5))
        margin_bottom = cm_to_pt(margins.get("margin_bottom_cm", 1.5))

        requests_list = []

        # 1. Update Document Margins
        requests_list.append({
            "updateDocumentStyle": {
                "documentStyle": {
                    "marginLeft": {"magnitude": margin_left, "unit": "PT"},
                    "marginRight": {"magnitude": margin_right, "unit": "PT"},
                    "marginTop": {"magnitude": margin_top, "unit": "PT"},
                    "marginBottom": {"magnitude": margin_bottom, "unit": "PT"},
                },
                "fields": "marginLeft,marginRight,marginTop,marginBottom",
            }
        })

        # 2. Update Columns (Section Style)
        enable_columns = doc_settings.get("enable_columns", False)
        if enable_columns:
            spacing_pt = cm_to_pt(doc_settings.get("column_spacing_cm", 0.5))
            separator_style = (
                "BETWEEN_EACH_COLUMN"
                if doc_settings.get("line_between_columns", True)
                else "NONE"
            )

            # Two columns: column 1 has right padding = spacing_pt
            column_props = [
                {"paddingEnd": {"magnitude": spacing_pt, "unit": "PT"}},
                {"paddingEnd": {"magnitude": 0.0, "unit": "PT"}},
            ]

            requests_list.append({
                "updateSectionStyle": {
                    "range": {"startIndex": 0, "endIndex": 1},
                    "sectionStyle": {
                        "columnProperties": column_props,
                        "columnSeparatorStyle": separator_style,
                    },
                    "fields": "columnProperties,columnSeparatorStyle",
                }
            })
        else:
            # Single column (default)
            requests_list.append({
                "updateSectionStyle": {
                    "range": {"startIndex": 0, "endIndex": 1},
                    "sectionStyle": {
                        "columnProperties": [{"paddingEnd": {"magnitude": 0.0, "unit": "PT"}}],
                        "columnSeparatorStyle": "NONE",
                    },
                    "fields": "columnProperties,columnSeparatorStyle",
                }
            })

        try:
            self.service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests_list}
            ).execute()
            logging.info(
                f"Applied layout: Margins [L/R: {margins.get('margin_left_cm', 1.5)}cm], "
                f"Columns: {'2 (with line)' if enable_columns else '1'}"
            )
        except Exception as e:
            logging.warning(f"Could not apply layout styling to doc: {e}")

    @staticmethod
    def _sanitize(text: str) -> str:
        if not text:
            return ""
        text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufeff]", "", text)

    def get_document_length(self, doc_id: str) -> int:
        try:
            doc = self.service.documents().get(documentId=doc_id).execute()
            content = doc.get("body", {}).get("content", [])
            if not content:
                return 1
            return max(1, content[-1].get("endIndex", 2) - 1)
        except Exception as e:
            logging.error(f"Error checking document size: {e}")
            return 1


    def append_structured_chapters(self, doc_id: str, chapters: List[Dict[str, Any]], font_size: int = 12):
        """
        Appends chapters with Breadcrumbs as Heading 1, Title as Heading 2,
        and applies paragraph styles so they register in Google Docs Navigation.
        """
        doc = self.service.documents().get(documentId=doc_id).execute()
        content = doc.get("body", {}).get("content", [])
        current_idx = max(1, content[-1].get("endIndex", 2) - 1) if content else 1

        batch_text = ""
        style_ranges = []

        for ch in chapters:
            bc_line = f"{ch['breadcrumb']}\n"
            title_line = f"{ch['title']}\n"

            # Optional source link
            source_line = f"ಮೂಲ ಕೊಂಡಿ: {ch['source_url']}\n" if ch.get("source_url") else ""
            divider_line = "---------------------------------------------------------\n\n"
            body_text = f"{ch['content']}\n\n"

            # Calculate precise absolute character offsets
            bc_start = current_idx + len(batch_text)
            bc_end = bc_start + len(bc_line)

            title_start = bc_end
            title_end = title_start + len(title_line)

            # Record styling ranges
            style_ranges.append({
                "type": "HEADING_1",
                "start": bc_start,
                "end": bc_end
            })
            style_ranges.append({
                "type": "HEADING_2",
                "start": title_start,
                "end": title_end
            })

            batch_text += bc_line + title_line + source_line + divider_line + body_text

        sanitized_batch = self._sanitize(batch_text)
        if not sanitized_batch.strip():
            return

        # 1. Insert complete text payload
        requests_payload = [
            {
                "insertText": {
                    "location": {"index": current_idx},
                    "text": sanitized_batch,
                }
            },
            {
                # Set body default font size
                "updateTextStyle": {
                    "range": {
                        "startIndex": current_idx,
                        "endIndex": current_idx + len(sanitized_batch),
                    },
                    "textStyle": {
                        "fontSize": {"magnitude": font_size, "unit": "PT"}
                    },
                    "fields": "fontSize",
                }
            }
        ]

        # 2. Apply Heading 1 and Heading 2 paragraph styles
        for sr in style_ranges:
            requests_payload.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": sr["start"],
                        "endIndex": sr["end"],
                    },
                    "paragraphStyle": {
                        "namedStyleType": sr["type"],
                    },
                    "fields": "namedStyleType",
                }
            })

        for attempt in range(3):
            try:
                self.service.documents().batchUpdate(
                    documentId=doc_id, body={"requests": requests_payload}
                ).execute()
                break
            except Exception as e:
                logging.warning(f"Batch styling failed at index {current_idx}: {e}")
                time.sleep(2 * (attempt + 1))
                if attempt == 2:
                    raise e


    def append_text(self, doc_id: str, text: str, font_size: int = 12):
        cleaned = self._sanitize(text)
        if not cleaned.strip():
            return
        if not cleaned.endswith("\n"):
            cleaned += "\n"

        CHUNK_SIZE = 30000
        chunks = [cleaned[i : i + CHUNK_SIZE] for i in range(0, len(cleaned), CHUNK_SIZE)]

        for chunk in chunks:
            insert_idx = self.get_document_length(doc_id)
            end_idx = insert_idx + len(chunk)

            requests_payload = [
                {
                    "insertText": {
                        "location": {"index": insert_idx},
                        "text": chunk,
                    }
                },
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": insert_idx,
                            "endIndex": end_idx,
                        },
                        "textStyle": {
                            "fontSize": {
                                "magnitude": font_size,
                                "unit": "PT",
                            }
                        },
                        "fields": "fontSize",
                    }
                },
            ]

            for attempt in range(3):
                try:
                    self.service.documents().batchUpdate(
                        documentId=doc_id, body={"requests": requests_payload}
                    ).execute()
                    break
                except Exception as e:
                    logging.warning(f"Retry {attempt+1}/3 failed inserting at {insert_idx}: {e}")
                    time.sleep(2 * (attempt + 1))
                    if attempt == 2:
                        raise e

class MediaWikiTreeCrawler:
    """Crawler adhering to Wikimedia rate limits."""

    API_URL = "https://sa.wikisource.org/w/api.php"
    HEADERS = {
        "User-Agent": "WikisourceIndicDocs/3.0 (IndicTextPipeline; contact: user@example.com)",
        "Accept-Encoding": "gzip",
    }

    def __init__(self, root_title: str):
        self.root_title = root_title
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        retries = Retry(
            total=5,
            backoff_factor=3,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def _safe_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        while True:
            res = self.session.get(self.API_URL, params=params, timeout=30)
            if res.status_code == 429:
                wait_sec = int(res.headers.get("Retry-After", 10))
                logging.warning(f"429 Hit. Cooling down for {wait_sec}s...")
                time.sleep(wait_sec)
                continue
            res.raise_for_status()
            return res.json()

    def get_all_subpages(self, prefix: str) -> List[str]:
        subpages: List[str] = []
        apfrom = ""
        while True:
            params = {
                "action": "query",
                "list": "allpages",
                "apprefix": prefix,
                "aplimit": 50,
                "format": "json",
            }
            if apfrom:
                params["apcontinue"] = apfrom

            data = self._safe_request(params)
            pages = data.get("query", {}).get("allpages", [])
            for p in pages:
                subpages.append(p["title"])

            if "continue" in data:
                apfrom = data["continue"].get("apcontinue", "")
                time.sleep(1.0)
            else:
                break
        return subpages

    def fetch_page_content(self, title: str) -> Dict[str, Any]:
        params = {
            "action": "parse",
            "page": title,
            "prop": "text|displaytitle",
            "format": "json",
            "disableeditsection": 1,
            "disabletoc": 1,
        }
        data = self._safe_request(params)
        if "error" in data:
            return {"title": title, "breadcrumbs": [], "content": "", "is_empty": True}

        raw_html = data.get("parse", {}).get("text", {}).get("*", "")
        soup = BeautifulSoup(raw_html, "html.parser")

        breadcrumbs: List[str] = []
        subpages_div = soup.find("div", class_="subpages")
        if subpages_div:
            for a in subpages_div.find_all("a"):
                t = a.get_text(strip=True)
                if t:
                    breadcrumbs.append(t)
            subpages_div.decompose()

        if not breadcrumbs and "/" in title:
            breadcrumbs = title.split("/")[:-1]

        for tag in soup.find_all(["table", "style", "script", "noscript"]):
            tag.decompose()

        for br in soup.find_all("br"):
            br.replace_with("\n")
        for block in soup.find_all(["p", "div"]):
            block.append("\n")

        cleaned = soup.get_text()
        lines = [line.strip() for line in cleaned.splitlines()]
        content = "\n".join(line for line in lines if line)

        return {
            "title": title,
            "breadcrumbs": breadcrumbs,
            "content": content,
            "is_empty": len(content) < 10,
        }

    def build_traversal_list(self) -> List[str]:
        logging.info(f"Scanning subpages under '{self.root_title}'...")
        all_pages = self.get_all_subpages(self.root_title)
        leaf_pages = [p for p in all_pages if p.count("/") >= 2]
        if not leaf_pages:
            leaf_pages = [p for p in all_pages if p != self.root_title]
        leaf_pages.sort()
        logging.info(f"Discovered {len(leaf_pages)} child leaf pages.")
        return leaf_pages


def identify_volume_for_page(page_title: str, breadcrumbs: List[str], volume_groups: List[Dict[str, Any]]) -> Dict[str, str]:
    """Matches a page to a volume configuration based on configured regex/substring patterns."""
    search_target = f"{' / '.join(breadcrumbs)} / {page_title}"

    for group in volume_groups:
        patterns = group.get("patterns", [])
        for pat in patterns:
            if pat in search_target:
                group_id = str(group.get("id") or "vol_general")
                title_suffix = str(group.get("title_suffix") or group_id)
                return {
                    "id": group_id,
                    "title_suffix": title_suffix,
                }

    return {
        "id": "vol_general",
        "title_suffix": "ಇತರೆ ಭಾಗಗಳು (General / Appendices)",
    }


def run_pipeline():
    global SHUTDOWN_REQUESTED

    cfg = ConfigManager.load_config()
    wikisource_url = cfg["wikisource_url"]
    output_doc_name = cfg["output_doc_name"]
    doc_settings = cfg["document_settings"]
    t_params = cfg["transliterate_params"]
    volume_groups = cfg["volume_groups"]
    c_settings = cfg["crawler_settings"]

    font_size = doc_settings.get("font_size", 12)
    include_url = doc_settings.get("include_referred_url", True)

    req_delay = c_settings["request_delay_seconds"]
    batch_size = c_settings["batch_size"]
    cooldown = c_settings["batch_cooldown_seconds"]
    max_pages = c_settings["max_pages"]

    path = urllib.parse.urlparse(wikisource_url).path
    title_encoded = path.split("/wiki/")[1] if "/wiki/" in path else path.lstrip("/")
    root_title = urllib.parse.unquote(title_encoded).replace("_", " ")

    state = StateManager.load_state()
    volume_docs: Dict[str, str] = state.get("volume_docs", {})
    processed_pages: List[str] = state.get("processed_pages", [])

    crawler = MediaWikiTreeCrawler(root_title)
    all_leaf_pages = crawler.build_traversal_list()

    if max_pages:
        logging.info(f"Limiting execution to {max_pages} pages as requested.")
        all_leaf_pages = all_leaf_pages[:max_pages]

    engine = LocalAksharamukhaEngine(t_params)
    docs_mgr = GoogleDocsManager()

    kn_root = engine.process(root_title)
    base_title = output_doc_name if output_doc_name else f"{kn_root}"

    pending_pages = [p for p in all_leaf_pages if p not in processed_pages]
    logging.info(
        f"Status: Total: {len(all_leaf_pages)} | Completed: {len(processed_pages)} | Remaining: {len(pending_pages)}"
    )

    # Buffers categorized by volume ID: {vol_id: [section_data, ...]}
    volume_buffers: Dict[str, List[Dict[str, Any]]] = {}
    batch_counter = 0
    run_folder_id = None

    for i, page_title in enumerate(pending_pages, 1):
        if SHUTDOWN_REQUESTED:
            logging.info("Shutting down cleanly. Progress saved.")
            break

        logging.info(f"[{i}/{len(pending_pages)}] Processing: {page_title}")
        page_data = crawler.fetch_page_content(page_title)

        if not page_data["is_empty"]:
            vol_info = identify_volume_for_page(page_title, page_data["breadcrumbs"], volume_groups)
            vol_id = vol_info["id"]

            if vol_id not in volume_docs:
                vol_doc_title = f"{base_title} - {vol_info['title_suffix']}"
                if run_folder_id is None:
                    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
                    run_folder_name = f"{base_title} - {timestamp}"
                    run_folder_id = docs_mgr.create_folder(run_folder_name)
                new_doc_id = docs_mgr.create_document(
                    vol_doc_title,
                    doc_settings=doc_settings,
                    parent_folder_id=run_folder_id,
                )
                volume_docs[vol_id] = new_doc_id

                header = f"{kn_root}\n"
                header += f"{vol_info['title_suffix']}\n"
                if include_url:
                    header += f"ಮೂಲ ಕೊಂಡಿ: {wikisource_url}\n"
                header += "=========================================================\n\n"
                docs_mgr.append_text(new_doc_id, header, font_size=font_size)
                StateManager.save_state(volume_docs, processed_pages)

            # 1. Clean Title: strip parent breadcrumb paths and any '#' markdown characters
            raw_title = page_title.split("/")[-1].strip()  # Only leaf chapter name (e.g., अध्यायः ०१)
            raw_title = re.sub(r"[#]+", "", raw_title).strip()
            kn_title = engine.process(raw_title)

            # 2. Breadcrumbs: transliterate trail
            kn_breadcrumbs = [engine.process(b) for b in page_data["breadcrumbs"]]
            breadcrumb_text = " > ".join(kn_breadcrumbs) if kn_breadcrumbs else kn_title
            breadcrumb_text = re.sub(r"[#]+", "", breadcrumb_text).strip()

            # 3. Transliterate Body Verses
            kn_content = engine.process(page_data["content"])

            # 4. Assemble structured section with metadata
            section_data = {
                "breadcrumb": breadcrumb_text,
                "title": kn_title,
                "source_url": f"https://sa.wikisource.org/wiki/{urllib.parse.quote(page_title.replace(' ', '_'))}" if include_url else None,
                "content": kn_content
            }

            if vol_id not in volume_buffers:
                volume_buffers[vol_id] = []
            volume_buffers[vol_id].append(section_data)

        processed_pages.append(page_title)
        batch_counter += 1

        if batch_counter >= batch_size or i == len(pending_pages):
            for v_id, sections_list in volume_buffers.items():
                if sections_list:
                    doc_id = volume_docs[v_id]
                    logging.info(f"Writing {len(sections_list)} chapters to Volume [{v_id}]...")
                    docs_mgr.append_structured_chapters(doc_id, sections_list, font_size=font_size)

            volume_buffers.clear()
            StateManager.save_state(volume_docs, processed_pages)
            batch_counter = 0

            if i < len(pending_pages):
                logging.info(f"Cooling pause ({cooldown}s)...")
                time.sleep(cooldown)
        else:
            time.sleep(req_delay)

    logging.info("\nPipeline Finished. Generated Volume Documents:")
    for v_id, doc_id in volume_docs.items():
        logging.info(f" • Volume [{v_id}]: https://docs.google.com/document/d/{doc_id}/edit")


if __name__ == "__main__":
    run_pipeline()