# SteelFind

A practical drawing desk for steel erection: upload PDF plans, search beam/bracket/angle marks across sheets, highlight their labels and save piece status and site notes.

## Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000. Windows: activate with `.venv\Scripts\activate`.

Upload the erection and angles plans under the same project, then search `AN22020`, `BRK22040` or just `22020`. Results link matching labels across all uploaded drawings. Use the zoom controls for detail and the PDF link to view/download the original vector PDF.

## Docker / VPS

```bash
docker compose up -d --build
```

The app is bound to localhost:8000. The `drawings` volume retains PDFs, the search index and notes. Back up the entire volume together. Add authentication at your reverse proxy (e.g. Caddy basic authentication) before allowing remote access. This first version has no built-in accounts; anyone with access can read uploaded drawings and edit notes. Do not expose the port directly to the Internet. Set your proxy request limit to at least 60 MB and upload timeout to at least 180 seconds.

## Features

- Multi-file PDF upload, project folders and drawing register.
- Search across every page, case-insensitive full or partial marks.
- Beam, bracket, angle and other common prefixes: B, BRK, AN, TR, SP, SC, C, P.
- Label highlights, page navigation, zoom, original PDF download.
- Persistent notes and statuses: Not started, Delivered, Installed, Issue.
- Notes are shared for the same mark within the same project.
- PDFs stay on the app server. No third-party document processing.
- Phone-friendly interface; SQLite and local file storage.

## Current limits

Text-based PDFs only; OCR is not included. The app locates label text rather than inferring the physical member or connection. Uploaded revisions remain separate drawings: it does not yet determine the latest revision or mark older sheets superseded. Section linking is by matching labels, not automatic section-boundary detection. No offline mode, photos, cloud sync, user roles or automatic drawing metadata extraction yet. Search shows at most 1,000 label hits. Up to 60 MB per request and 100 pages per PDF. Very detailed drawings may require the original PDF for fine print.

No customer drawings or extracted project data are committed to this repository. Upload files after starting the app.

## Verify

```bash
python -m unittest discover -s tests -v
```
