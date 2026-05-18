---
name: paper_reader
description: >
  Parse and analyze academic papers from PDF files, generate structured 
  JSON summaries and save to docs/papers directory.
trigger_keywords: ["读论文", "分析PDF", "总结paper", "read this paper", "analyze PDF"]
---
# Paper Reader

## Overview

Read academic papers from PDF files, extract structured information, and save as JSON to the papers directory.

## Input

| Parameter | Type | Description |
|-----------|------|-------------|
| `pdf_path` | string | Local file path to the PDF |

## Output JSON Structure

```json
{
  "metadata": {
    "title": "Paper title",
    "authors": ["Author 1", "Author 2"],
    "year": "2024",
    "venue": "Journal/Conference name",
    "pdf_path": "path/to/file.pdf"
  },
  "abstract": "2-3 sentence summary",
  "key_contributions": [
    "Contribution 1",
    "Contribution 2"
  ],
  "methodology": "Brief description of approach",
  "key_findings": [
    "Finding 1",
    "Finding 2"
  ],
  "limitations": [
    "Limitation 1"
  ],
  "saved_at": "2026-01-01"
}
```

## Workflow

### Step 1: Setup Directories
- Scripts directory: `.\LabPilot\tmp`
- Papers directory: `.\LabPilot\docs\papers`
- Create directories if not exist

### Step 2: Copy PDF
- Copy original PDF to `.\LabPilot\docs\papers`
- Use original filename or generate based on paper title

### Step 3: Extract Content
- Use pypdf to read PDF text
- Save extracted text to `.\LabPilot\tmp` for analysis

### Step 4: Generate JSON
- Parse extracted text to extract structured information
- Generate JSON file with same name as PDF (e.g., `paper_title.json`)

### Step 5: Save to Fact Library
- Call `remember_fact()` to save key insights to long-term fact library
- Use structured JSON content as the fact value
- Do NOT use core_block functions (save_core_block, append_core_block, etc.)

### Step 6: Save JSON File
- Save JSON to `.\LabPilot\docs\papers`
- Clean up temp files in `.\LabPilot\tmp`

## Directory Structure

```
.\LabPilot\
├── tmp\                    # Python scripts and temp files
│   ├── extract_pdf.py
│   └── page_*.txt
└── docs\
    └── papers\             # Original PDFs and JSON summaries
        ├── paper1.pdf
        ├── paper1.json
        └── paper2.json
```

## Important Constraints

| # | Constraint | Description |
|---|------------|-------------|
| 1 | **Python Interpreter** | Use `D:\PDHlocking\LabPilot\venv\Scripts\python.exe` |
| 2 | **Working Directory** | All operations within `D:\PDHlocking\LabPilot` |
| 3 | **Script Location** | Scripts run from `.\LabPilot\tmp` |
| 4 | **Paper Location** | Save to `.\LabPilot\docs\papers` |
| 5 | **Output Format** | JSON only |
| 6 | **Fact Library** | Use `remember_fact()` for long-term storage |
| 7 | **No Core Blocks** | Do NOT use core_block functions (save_core_block, append_core_block, etc.) |

## Technical Notes

### PDF Extraction Script Template

```python
# extract_pdf.py - save to .\LabPilot\tmp
from pypdf import PdfReader
import sys
import os

pdf_path = sys.argv[1] if len(sys.argv) > 1 else r"PATH_TO_PDF"
output_dir = r"D:\PDHlocking\LabPilot\tmp"

reader = PdfReader(pdf_path)
print(f"Total pages: {len(reader.pages)}")

for i, page in enumerate(reader.pages):
    text = page.extract_text()
    output_file = os.path.join(output_dir, f"page_{i+1}.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Extracted page {i+1}")
```

### File Path Handling
- Use raw strings (r"...") for paths with spaces
- Always use absolute paths starting from D:\PDHlocking

## Response Handling

| Scenario | Behavior |
|----------|----------|
| PDF not found | Return error with suggestion to check path |
| Encrypted PDF | Notify user |
| Very long paper (>50 pages) | Focus on abstract, intro, conclusion, key results |