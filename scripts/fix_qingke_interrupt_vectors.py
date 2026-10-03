from __future__ import annotations

import argparse
import re
from pathlib import Path


EXTERNAL_INTERRUPT_OFFSET = 16
LINK_SECTION = ".vector_table.external_interrupts"


VECTOR_DECLARATION_PATTERN = re.compile(
    r"pub static __EXTERNAL_INTERRUPTS: "
    r"\[Vector; (?P<count>\d+)\] = \["
)
RESERVED_VECTOR_PATTERN = re.compile(
    r"\s*Vector\s*\{\s*_reserved:\s*0\s*,?\s*\},"
)
VECTOR_ENTRY_PATTERN = re.compile(
    r"Vector\s*\{\s*"
    r"(?:(?:_handler:\s*[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?:_reserved:\s*\d+))"
    r"\s*,?\s*\},",
    re.DOTALL,
)


def fix_interrupt_vectors(path: Path) -> None:
    original = path.read_text(encoding="utf-8")
    fixed, vector_count, removed = remove_qingke_core_vectors(original)
    fixed = add_link_section(fixed)

    if fixed == original:
        print(f"[QINGKE/PAC] already fixed: {path}")
        return

    path.write_text(fixed, encoding="utf-8")

    print(
        "[QINGKE/PAC] fixed external interrupt vectors"
        f" | file={path}"
        f" | removed-leading-reserved={removed}"
        f" | vectors={vector_count}"
        f" | link-section={LINK_SECTION}"
    )


def remove_qingke_core_vectors(source: str) -> tuple[str, int, int]:
    declaration = VECTOR_DECLARATION_PATTERN.search(source)

    if declaration is None:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS declaration was not found"
        )

    vector_count = int(declaration.group("count"))
    body_start = declaration.end()
    body_end = source.find("\n];", body_start)

    if body_end < 0:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS closing ]; was not found"
        )

    body = source[body_start:body_end]
    entries = list(VECTOR_ENTRY_PATTERN.finditer(body))

    if len(entries) != vector_count:
        raise RuntimeError(
            "Could not parse every __EXTERNAL_INTERRUPTS entry: "
            f"parsed {len(entries)}, declaration says {vector_count}"
        )

    cursor = 0

    for _ in range(EXTERNAL_INTERRUPT_OFFSET):
        match = RESERVED_VECTOR_PATTERN.match(body, cursor)

        if match is None:
            if has_link_section(source, declaration.start()):
                return source, vector_count, 0

            raise RuntimeError(
                "The first 16 entries of __EXTERNAL_INTERRUPTS are not "
                "QingKe core-reserved vectors, and the table is not marked "
                "as already fixed"
            )

        cursor = match.end()

    fixed_count = vector_count - EXTERNAL_INTERRUPT_OFFSET

    if fixed_count <= 0:
        raise RuntimeError(
            f"Invalid external interrupt vector count: {vector_count}"
        )

    remaining_body = body[cursor:]
    new_declaration = declaration.group(0).replace(
        f"[Vector; {vector_count}]",
        f"[Vector; {fixed_count}]",
    )
    new_body = "\n" + remaining_body.lstrip("\r\n")

    fixed = (
        source[:declaration.start()]
        + new_declaration
        + new_body
        + source[body_end:]
    )

    return fixed, fixed_count, EXTERNAL_INTERRUPT_OFFSET


def has_link_section(source: str, declaration_start: int) -> bool:
    attribute = f'#[link_section = "{LINK_SECTION}"]'
    preceding = source[max(0, declaration_start - 512):declaration_start]
    return attribute in preceding


def add_link_section(source: str) -> str:
    declaration = VECTOR_DECLARATION_PATTERN.search(source)

    if declaration is None:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS declaration disappeared during processing"
        )

    if has_link_section(source, declaration.start()):
        return source

    attribute = f'#[link_section = "{LINK_SECTION}"]'

    return (
        source[:declaration.start()]
        + attribute
        + "\n"
        + source[declaration.start():]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fix svd2rust RISC-V external interrupt tables for QingKe by "
            "removing the 16 core-vector slots from __EXTERNAL_INTERRUPTS."
        )
    )

    parser.add_argument(
        "mod_rs",
        type=Path,
        help="Path to the generated PAC mod.rs",
    )

    args = parser.parse_args()

    if not args.mod_rs.is_file():
        raise FileNotFoundError(args.mod_rs)

    fix_interrupt_vectors(args.mod_rs)


if __name__ == "__main__":
    main()
