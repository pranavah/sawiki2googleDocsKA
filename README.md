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

## Configuration

Set the source page and output document name in `parameters.yml`. The same file
controls transliteration (`src` and `tgt`), volume grouping, source-link
inclusion, page limits, and Google Docs formatting.

Google Docs and Drive access requires OAuth credentials in `credentials.json`;
the authorized token is stored in `token.json`.
