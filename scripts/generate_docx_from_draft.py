import os

from docx import Document


def txt_to_docx(txt_path: str, docx_path: str) -> None:
    """
    将纯文本论文草稿转换为 docx 文档。

    简单策略：
    - 每一行作为一个段落；
    - 空行用于分段；
    后续可在 Word 中手动应用“标题 1/2/3”等样式。
    """
    document = Document()

    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            # 去掉末尾换行，但保留前后空格（有些行可能有刻意缩进）
            stripped = line.rstrip("\n")
            # 直接写入为一个段落
            document.add_paragraph(stripped)

    # 确保输出目录存在
    os.makedirs(os.path.dirname(docx_path) or ".", exist_ok=True)
    document.save(docx_path)


def main():
    # Script may be in project root or in scripts/; draft lives in docs/
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir) if os.path.basename(script_dir) == "scripts" else script_dir
    docs_dir = os.path.join(base_dir, "docs")
    txt_path = os.path.join(docs_dir, "草稿-20260205")
    docx_path = os.path.join(docs_dir, "草稿-20260205.docx")

    if not os.path.exists(txt_path):
        raise FileNotFoundError(f"找不到源文件: {txt_path}")

    txt_to_docx(txt_path, docx_path)
    print(f"已生成 docx 文件: {docx_path}")


if __name__ == "__main__":
    main()

