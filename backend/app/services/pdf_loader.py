import fitz


def extract_pdf_text(pdf_path):

    try:

        doc = fitz.open(pdf_path)

        text = ""

        for page in doc:

            try:

                text += page.get_text(
                    "text"
                )

                text += "\n"

            except Exception:

                continue

        doc.close()

        return text

    except Exception as e:

        print(
            f"PDF Text Extraction Error: {e}"
        )

        return ""


def extract_pdf_pages(pdf_path):

    try:

        doc = fitz.open(pdf_path)

        pages = []

        for page_num, page in enumerate(
            doc,
            start=1
        ):

            try:

                page_text = page.get_text(
                    "text"
                )

                pages.append(

                    {
                        "page":
                        page_num,

                        "text":
                        page_text
                    }
                )

            except Exception:

                pages.append(

                    {
                        "page":
                        page_num,

                        "text":
                        ""
                    }
                )

        doc.close()

        return pages

    except Exception as e:

        print(
            f"PDF Page Extraction Error: {e}"
        )

        return []