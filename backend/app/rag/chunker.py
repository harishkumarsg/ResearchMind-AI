from langchain_text_splitters import (
    RecursiveCharacterTextSplitter
)


def create_chunks(text):

    splitter = RecursiveCharacterTextSplitter(

        chunk_size=1500,

        chunk_overlap=300,

        separators=[
            "\n\n",
            "\n",
            ". ",
            " ",
            ""
        ]
    )

    return splitter.split_text(text)