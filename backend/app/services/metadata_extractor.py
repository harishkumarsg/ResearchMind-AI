import re


# ---------------------------------------
# ABSTRACT
# ---------------------------------------

def extract_abstract(text):

    match = re.search(
        r"abstract\s*(.*?)(keywords|index terms)",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if match:

        abstract = match.group(1)

        abstract = re.sub(
            r"\s+",
            " ",
            abstract
        )

        return abstract[:2500]

    return ""


# ---------------------------------------
# KEYWORDS
# ---------------------------------------

def extract_keywords(text):

    match = re.search(
        r"(keywords|index terms)\s*[:-]?\s*(.*)",
        text,
        re.IGNORECASE
    )

    if match:

        keywords = match.group(2)

        keywords = re.sub(
            r"\s+",
            " ",
            keywords
        )

        return keywords[:500]

    return ""


# ---------------------------------------
# AUTHORS
# ---------------------------------------

def extract_authors(text):

    lines = text.split("\n")

    for line in lines[:25]:

        line = line.strip()

        if len(line) < 5:
            continue

        if "@" in line:
            continue

        lower = line.lower()

        if any(
            x in lower
            for x in [
                "department",
                "university",
                "college",
                "school",
                "faculty",
                "abstract",
                "keywords",
                "index terms",
                "institute",
                "technology",
                "engineering"
            ]
        ):
            continue

        words = line.split()

        if 2 <= len(words) <= 15:

            capitals = sum(
                word[0].isupper()
                for word in words
                if word and word[0].isalpha()
            )

            if capitals >= 2:

                return line

    return "Unknown Authors"