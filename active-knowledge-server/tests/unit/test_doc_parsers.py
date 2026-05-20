from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from active_knowledge_server.parsers.api_docs import parse_api_doc
from active_knowledge_server.parsers.markdown import parse_html_document, parse_markdown_document
from active_knowledge_server.parsers.widget_docs import parse_widget_doc


def test_parse_api_markdown_front_matter_and_heading_chunks() -> None:
    document = parse_api_doc(
        Path("knowledge-sources/api/sensor.md"),
        text=dedent(
            """\
            ---
            module: sensors
            version: v2
            code_symbols:
              - sensor_register
              - sensor_attach
            profiles:
              - default
            authority_level: normative
            ---
            # Sensor API

            Intro line.

            ## Register
            Call sensor_register().

            ## Attach
            Call sensor_attach().
            """
        ),
    )

    assert document.format == "markdown"
    assert document.category == "api"
    assert document.title == "Sensor API"
    assert document.front_matter is not None
    assert document.front_matter.known_fields == {
        "module": "sensors",
        "version": "v2",
        "code_symbols": ["sensor_register", "sensor_attach"],
        "profiles": ["default"],
        "authority_level": "normative",
    }
    assert [heading.path for heading in document.headings] == [
        ("Sensor API",),
        ("Sensor API", "Register"),
        ("Sensor API", "Attach"),
    ]
    assert [(chunk.start_line, chunk.end_line) for chunk in document.chunks] == [
        (11, 13),
        (15, 16),
        (18, 19),
    ]
    assert "Intro line." in document.chunks[0].text


def test_parse_widget_markdown_front_matter() -> None:
    document = parse_widget_doc(
        Path("knowledge-sources/widgets/button.md"),
        text=dedent(
            """\
            ---
            widget: active-button
            ui_framework: lvgl
            code_paths:
              - ui/button.c
              - ui/button.h
            tags: [input, touch]
            authority_level: curated
            ---
            # Active Button

            Button docs.
            """
        ),
    )

    assert document.category == "widgets"
    assert document.front_matter is not None
    assert document.front_matter.known_fields == {
        "widget": "active-button",
        "ui_framework": "lvgl",
        "code_paths": ["ui/button.c", "ui/button.h"],
        "tags": ["input", "touch"],
        "authority_level": "curated",
    }
    assert document.chunks[0].start_line == 10
    assert document.chunks[0].end_line == 12


def test_parse_product_markdown_preserves_extension_fields() -> None:
    document = parse_markdown_document(
        Path("knowledge-sources/product/roadmap.md"),
        dedent(
            """\
            ---
            authority_level: draft
            owner: pm-core
            milestone: 2026Q3
            ---
            # Roadmap

            Roadmap body.
            """
        ),
        category="product",
    )

    assert document.front_matter is not None
    assert document.front_matter.known_fields == {"authority_level": "draft"}
    assert document.front_matter.extension_fields == {
        "owner": "pm-core",
        "milestone": "2026Q3",
    }


def test_parse_html_extracts_title_body_table_and_line_numbers() -> None:
    document = parse_html_document(
        Path("knowledge-sources/engineering/runtime.html"),
        dedent(
            """\
            <html>
              <head><title>Runtime Guide</title></head>
              <body>
                <h1>Runtime Guide</h1>
                <p>Queue handling overview.</p>
                <h2>Latency</h2>
                <p>Measure end-to-end latency.</p>
                <table>
                  <tr><th>Metric</th><th>Value</th></tr>
                  <tr><td>P95</td><td>12ms</td></tr>
                </table>
              </body>
            </html>
            """
        ),
        category="engineering",
    )

    assert document.format == "html"
    assert document.title == "Runtime Guide"
    assert [heading.path for heading in document.headings] == [
        ("Runtime Guide",),
        ("Runtime Guide", "Latency"),
    ]
    assert [(chunk.start_line, chunk.end_line) for chunk in document.chunks] == [
        (4, 5),
        (6, 10),
    ]
    assert "Metric | Value" in document.chunks[1].text
    assert "P95 | 12ms" in document.chunks[1].text