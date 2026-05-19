"""Vision-model fallback parser backed by the OpenAI Responses API."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import List

from openai import OpenAI

from docmark.models.document_schema import (
    BlockType,
    ContentBlock,
    ExtractionMethod,
    PageResult,
    TableContent,
)
from docmark.utils.pdf_utils import pdf_page_to_base64


IMAGE_PROMPT = """Convert this document page to clean Markdown format.

CRITICAL RULES:
1. Output ONLY the text and content visible on the page - nothing else
2. Do NOT add any commentary, descriptions, or explanations
3. Do NOT add labels like "TABLE:", "PROCEDURE:", "DIAGRAM:" etc.
4. Include ALL text: headers, footers, page numbers, document IDs, revision info

FORMATTING:
- Headers/titles: Use # ## ### based on visual hierarchy
- Tables: Convert to Markdown tables, preserve all cells exactly
- Lists: Use - or numbered lists matching the document
- Body text: Output as paragraphs

PRESERVE EXACTLY:
- Document numbers (e.g., F-MPROD-401, SOP-123)
- Revision numbers and dates
- All form fields and their values
- Signatures and dates
- Checkboxes as [ ] or [x]
- All measurements and units
- No emojis

Output the page content as clean Markdown. No additional text."""

IMAGE_DESCRIPTION_PROMPT = """Describe this embedded document image in 1-3 concise sentences.

Rules:
- Be factual and concise.
- Describe visible structure (diagram, chart, table, photo, signature, logo).
- Include important visible text if legible.
- Do not invent details.
- Output plain text only."""


class VisionModelParser:
    """Vision-model parser using the OpenAI Responses API.

    Used as the fallback for pages where Docling's confidence is too low,
    Docling fails outright, or the user has forced vision-only conversion.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini",
        dpi: int = 200,
        detail: str = "high",
    ):
        if not api_key:
            raise ValueError("OpenAI API key is required for vision fallback")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.dpi = dpi
        self.detail = detail

    def enhance_page(self, pdf_path: Path, page_num: int) -> PageResult:
        """Render a PDF page and convert it to Markdown via the vision model."""
        start = time.time()
        b64_image = pdf_page_to_base64(pdf_path, page_num, self.dpi)
        markdown_content = self._call_vision_api(b64_image, IMAGE_PROMPT)
        blocks = self._parse_markdown_to_blocks(markdown_content)

        return PageResult(
            page_num=page_num,
            extraction_method=ExtractionMethod.VISION_MODEL,
            confidence=0.95,
            processing_time_seconds=time.time() - start,
            blocks=blocks,
            warnings=[],
            fallback_triggered=True,
        )

    def describe_image_bytes(self, image_bytes: bytes, mime_type: str) -> str:
        """Generate a short description for an embedded image payload."""
        if not image_bytes:
            raise ValueError("Empty image bytes")
        if not mime_type:
            mime_type = "image/png"

        b64_image = base64.b64encode(image_bytes).decode("ascii")
        description = self._call_vision_api(
            b64_image, IMAGE_DESCRIPTION_PROMPT, mime_type
        ).strip()
        if not description:
            raise RuntimeError("Vision returned empty image description")
        return description

    def _call_vision_api(
        self, b64_image: str, prompt: str, mime_type: str = "image/png"
    ) -> str:
        resp = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{b64_image}",
                            "detail": self.detail,
                        },
                    ],
                }
            ],
        )
        return resp.output_text

    @staticmethod
    def _parse_markdown_to_blocks(markdown: str) -> List[ContentBlock]:
        """Parse vision-model markdown into structured ContentBlocks."""
        blocks: List[ContentBlock] = []
        lines = markdown.split("\n")

        i = 0
        while i < len(lines):
            line = lines[i]

            if line.startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                content = line.lstrip("#").strip()
                blocks.append(
                    ContentBlock(type=BlockType.HEADING, content=content, level=level)
                )
                i += 1
            elif "|" in line and i + 1 < len(lines) and "---" in lines[i + 1]:
                table_lines = [line]
                i += 1
                while i < len(lines) and "|" in lines[i]:
                    table_lines.append(lines[i])
                    i += 1
                table_md = "\n".join(table_lines)
                blocks.append(
                    ContentBlock(
                        type=BlockType.TABLE,
                        content=TableContent(
                            markdown=table_md,
                            caption=None,
                            extraction_confidence=0.95,
                            enhanced_by_vision=True,
                        ),
                    )
                )
            elif line.startswith("```"):
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                i += 1
                blocks.append(
                    ContentBlock(type=BlockType.CODE, content="\n".join(code_lines))
                )
            elif line.strip():
                blocks.append(ContentBlock(type=BlockType.TEXT, content=line))
                i += 1
            else:
                i += 1

        return blocks
