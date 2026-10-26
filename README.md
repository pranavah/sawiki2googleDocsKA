# Wikisource to Kannada Google Docs

This project prepares a Kannada-script edition of Sanskrit texts hosted on
[Sanskrit Wikisource](https://sa.wikisource.org/). It crawls the selected
Wikisource page and its chapter pages with the MediaWiki API, extracts the text,
and uses [Aksharamukha](https://aksharamukha.com/) to transliterate it from
Devanagari into Kannada script.

This is script transliteration, not translation: the Sanskrit wording is
preserved while its script is converted to Kannada.

## How it works

1. Reads the root Wikisource URL and processing options from `parameters.yml`.
2. Uses the Wikisource MediaWiki API to find child pages and fetch their text
   and chapter breadcrumbs.
3. Removes page markup and non-content elements, then transliterates the text,
   titles, and breadcrumbs with the configured Aksharamukha options.
4. Groups chapters into Google Docs volumes using the configured
   `volume_groups`; applies document layout settings such as columns and
   margins, and optionally includes source links.
5. Creates a Google Drive folder named
   `<output_doc_name> - yyyyMMdd-HHmm` and saves new volume documents there.

The crawler observes configured request delays and batch cooldowns. Progress is
stored in `crawler_state.json`, allowing already-created volume documents and
processed pages to be reused on a later run.

## Setup and execution
Install Python and ensure the `python` command is available on your Windows
`PATH`.

Install Python 3. On Linux, also ensure Bash is available.

### Google account setup (first run)

The first run needs an OAuth client configuration from your own Google Cloud
project; Google does not allow the app to sign in without this file:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project and enable the **Google Docs API** and **Google Drive API**.
2. Configure the OAuth consent screen. If the app is in testing mode, add the
   Google account you will use as a test user.
3. Create an OAuth client ID with application type **Desktop app**, download
   its JSON file, and save it in the project directory as `credentials.json`.

`credentials.json` contains OAuth client configuration and should not be
shared. It is excluded from Git.

On Windows, double-click `run_pipeline.bat`. On Linux, run:

```bash
bash run_pipeline.sh
```

Both launchers create a local `.venv` virtual environment if needed, install
the dependencies from `requirements.txt`, and start the crawler. If
`credentials.json` is missing, the program explains how to create it and
prompts you to add it before continuing. Google authorization happens before
the Wikisource crawl: a browser opens for you to sign in and approve the
requested Google Docs access and Drive file access needed to create folders
and documents for this app (it does not request unrestricted Drive access).
The authorization is saved locally in `token.json` and refreshed automatically
when possible. Both `token.json` and `credentials.json` are excluded from Git.

To install and run manually from PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\wikisource_crawler_daemon.py
```

To install and run manually on Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python wikisource_crawler_daemon.py
```

## Configuration

Set the source page and output document name in `parameters.yml`. The same file
controls transliteration (`src` and `tgt`), volume grouping, source-link
inclusion, page limits, and Google Docs formatting.
