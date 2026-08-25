from __future__ import annotations

import argparse
import re
from pathlib import Path


EXTERNAL_INTERRUPT_OFFSET = 16
EXPECTED_ORIGINAL_VECTOR_COUNT = 104
EXPECTED_FIXED_VECTOR_COUNT = (
    EXPECTED_ORIGINAL_VECTOR_COUNT - EXTERNAL_INTERRUPT_OFFSET
)

LINK_SECTION = ".vector_table.external_interrupts"


def fix_interrupt_vectors(path: Path) -> None:
    original = path.read_text(encoding="utf-8")

    declaration_pattern = re.compile(
        r"pub static __EXTERNAL_INTERRUPTS: "
        r"\[Vector; (?P<count>\d+)\] = \["
    )

    declaration = declaration_pattern.search(original)

    if declaration is None:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS declaration was not found"
        )

    vector_count = int(declaration.group("count"))

    body_start = declaration.end()
    body_end = original.find("\n];", body_start)

    if body_end < 0:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS closing ]; was not found"
        )

    body = original[body_start:body_end]

    if vector_count == EXPECTED_FIXED_VECTOR_COUNT:
        fixed = original

    elif vector_count == EXPECTED_ORIGINAL_VECTOR_COUNT:
        cursor = 0

        reserved_pattern = re.compile(
            r"\s*Vector\s*\{\s*_reserved:\s*0\s*\},"
        )

        for index in range(EXTERNAL_INTERRUPT_OFFSET):
            match = reserved_pattern.match(body, cursor)

            if match is None:
                raise RuntimeError(
                    "Expected reserved vector "
                    f"#{index} at the beginning of "
                    "__EXTERNAL_INTERRUPTS"
                )

            cursor = match.end()

        remaining_body = body[cursor:]

        first_handler = re.match(
            r"\s*Vector\s*\{\s*_handler:",
            remaining_body,
        )

        if first_handler is None:
            raise RuntimeError(
                "After removing the first 16 reserved vectors, "
                "the table does not start with an interrupt handler"
            )

        new_body = "\n" + remaining_body.lstrip("\r\n")

        old_declaration = declaration.group(0)
        new_declaration = old_declaration.replace(
            f"[Vector; {EXPECTED_ORIGINAL_VECTOR_COUNT}]",
            f"[Vector; {EXPECTED_FIXED_VECTOR_COUNT}]",
        )

        fixed = (
            original[:declaration.start()]
            + new_declaration
            + new_body
            + original[body_end:]
        )

    else:
        raise RuntimeError(
            "Unexpected __EXTERNAL_INTERRUPTS length: "
            f"{vector_count}, expected "
            f"{EXPECTED_ORIGINAL_VECTOR_COUNT} or "
            f"{EXPECTED_FIXED_VECTOR_COUNT}"
        )

    declaration = declaration_pattern.search(fixed)

    if declaration is None:
        raise RuntimeError(
            "__EXTERNAL_INTERRUPTS declaration disappeared "
            "during processing"
        )

    link_section_attribute = (
        f'#[link_section = "{LINK_SECTION}"]'
    )

    preceding_attributes = fixed[
        max(0, declaration.start() - 512):declaration.start()
    ]

    if link_section_attribute not in preceding_attributes:
        fixed = (
            fixed[:declaration.start()]
            + link_section_attribute
            + "\n"
            + fixed[declaration.start():]
        )

    if fixed == original:
        print(f"[QINGKE/PAC] already fixed: {path}")
        return

    path.write_text(fixed, encoding="utf-8")

    print(
        "[QINGKE/PAC] fixed external interrupt vectors"
        f" | file={path}"
        f" | removed-leading-reserved={EXTERNAL_INTERRUPT_OFFSET}"
        f" | vectors={EXPECTED_FIXED_VECTOR_COUNT}"
        f" | link-section={LINK_SECTION}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fix svd2rust RISC-V external interrupt table "
            "for QingKe."
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