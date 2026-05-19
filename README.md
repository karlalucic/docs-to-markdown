# DocMark

Drop a document on the window. Get clean Markdown next to the source file.

PDF, DOCX, PPTX, XLSX → `.md`.

## Why

LLMs and agents work best with Markdown:

- **Fewer tokens.** Markdown drops the layout noise that PDFs and Office formats carry. A 40-page PDF can shrink by 5–10× once it's text.
- **Cleaner structure.** Headings, lists, and tables survive — so retrieval, chunking, and citations stay accurate.
- **Agent-friendly.** Tools that read your files (Claude, ChatGPT, Cursor, IDE assistants, MCP servers) can parse `.md` directly. No OCR step, no PDF library, no surprises.

Use it for things like:

- Feeding research papers or whitepapers into a chat without burning context.
- Preparing a folder of SOPs, manuals, or contracts for a RAG index.
- Letting a coding agent read design docs or spec PDFs alongside your repo.
- Converting old DOCX/PPTX archives into something `grep` and AI both understand.
- Personal note-taking: turn anything you save into Markdown your editor can open.

## How it works

PDF conversion uses two tiers, in order:

1. **Docling** reads the document's text layer and structure. Fast, accurate, free.
2. **OpenAI vision fallback** (`gpt-4.1-mini` by default) kicks in only for pages Docling can't handle well — scanned PDFs, complex tables, technical diagrams.

Most native PDFs never touch the API.

DOCX, PPTX, and XLSX use lightweight native parsers in v1. PPTX can optionally use vision fallback for complex slides.

## Install

Requirements:

- Python 3.10+
- An OpenAI API key for scanned PDFs and vision fallback
- A working desktop webview backend (`pywebview` installs the Python side)
- Internet access on first Docling run if model assets are not already cached

```bash
python3 -m pip install -e .
docmark
```

Open Settings (gear icon, top right) and paste your OpenAI API key. The key is stored in your OS keychain — never on disk.

For `.ppt` (legacy PowerPoint) or PPTX vision fallback, install LibreOffice:

```bash
brew install --cask libreoffice          # macOS
sudo apt install libreoffice              # Linux
```

## Use

Drag one or more files onto the window. Each gets a `.md` written next to it:

```
~/Documents/report.pdf  →  ~/Documents/report.md
```

If `report.md` already exists, the new file is `report-1.md`.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Vision model | `gpt-4.1-mini` | Strong GPT-4-class value default; `gpt-5-mini` is worth benchmarking if you allow GPT-5 models |
| PDF render DPI | `200` | Raise for tiny text or noisy scans |
| Detail | `high` | `low` / `high` / `auto` |
| Force vision | off | Skip Docling, send every PDF page to the model |
| PPTX vision | off | Render slides through LibreOffice → image → vision |
| DOCX image descriptions | off | Vision-generated alt text for embedded images |
| Cache | on | Per-page results reused across runs |

## License

MIT.
