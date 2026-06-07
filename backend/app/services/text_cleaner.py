import re


def clean_text(text):

    # Normalize line endings
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove excessive spaces but keep newlines
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    # Remove page numbers
    text = re.sub(
        r"Page\s+\d+",
        "",
        text,
        flags=re.IGNORECASE
    )

    # Collapse multiple blank lines
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()