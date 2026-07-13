from pypdf import PdfReader, PdfWriter


def split_pdf(pdf_path, block_size):
    """Recorta um PDF em blocos de `block_size` páginas.

    Retorna uma lista de bytes, cada item sendo um sub-PDF válido.
    """
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    blocks = []
    for start in range(0, total_pages, block_size):
        end = min(start + block_size, total_pages)
        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])

        from io import BytesIO
        buffer = BytesIO()
        writer.write(buffer)
        blocks.append({
            "pdf_bytes": buffer.getvalue(),
            "start_page": start + 1,
            "end_page": end,
        })

    return blocks
