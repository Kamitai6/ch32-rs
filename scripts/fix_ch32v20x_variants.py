from __future__ import annotations

import argparse
import re
from pathlib import Path


D6_FEATURE = "ch32v20x-d6"
D8_FEATURE = "ch32v20x-d8"
BASE_FEATURE = "ch32v20x"

EXTERNAL_INTERRUPT_OFFSET = 16
D6_VECTOR_COUNT = 46
D8_VECTOR_COUNT = 54

RUST_MARKER = "// CH32V20X_VARIANT_INTERRUPTS"
README_MARKER = "<!-- CH32V20X_VARIANT_FEATURES -->"
LINK_SECTION = ".vector_table.external_interrupts"

D8_ONLY_INTERRUPT_VALUES = {61, 62, 63, 64, 65, 67, 68, 69}

VECTOR_DECLARATION_PATTERN = re.compile(
    r"pub static __EXTERNAL_INTERRUPTS: "
    r"\[Vector; (?P<count>\d+)\] = \["
)
VECTOR_ENTRY_PATTERN = re.compile(
    r"Vector\s*\{\s*"
    r"(?:(?:_handler:\s*(?P<handler>[A-Za-z_][A-Za-z0-9_]*))|"
    r"(?:_reserved:\s*(?P<reserved>\d+)))"
    r"\s*,?\s*\},",
    re.DOTALL,
)
INTERRUPT_VARIANT_PATTERN = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<value>\d+),\s*$"
)
TRY_FROM_ARM_PATTERN = re.compile(
    r"^(?P<indent>\s*)(?P<value>\d+)\s*=>\s*"
    r"Ok\(Interrupt::(?P<name>[A-Za-z_][A-Za-z0-9_]*)\),\s*$"
)


def fix_ch32v20x_variants(crate_dir: Path) -> None:
    mod_rs = crate_dir / "src" / "ch32v20x" / "mod.rs"
    cargo_toml = crate_dir / "Cargo.toml"
    readme = crate_dir / "README.md"

    for path in (mod_rs, cargo_toml, readme):
        if not path.is_file():
            raise FileNotFoundError(path)

    rust_changed = patch_mod_rs(mod_rs)
    cargo_changed = patch_cargo_toml(cargo_toml)
    readme_changed = patch_readme(readme)

    print(
        "[CH32V20X/PAC] variant support ready"
        f" | crate={crate_dir}"
        f" | rust={'changed' if rust_changed else 'unchanged'}"
        f" | cargo={'changed' if cargo_changed else 'unchanged'}"
        f" | readme={'changed' if readme_changed else 'unchanged'}"
        f" | features={D6_FEATURE},{D8_FEATURE}"
    )


def patch_mod_rs(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")

    if RUST_MARKER in original:
        return False

    if f'#[link_section = "{LINK_SECTION}"]' not in original:
        raise RuntimeError(
            "QingKe vector correction has not been applied yet. Run "
            "fix_qingke_interrupt_vectors.py before this script."
        )

    fixed = split_vector_table(original)
    fixed = split_interrupt_module(fixed)
    fixed = insert_variant_checks(fixed)

    if fixed == original:
        return False

    path.write_text(fixed, encoding="utf-8")
    return True


def split_vector_table(source: str) -> str:
    declaration = VECTOR_DECLARATION_PATTERN.search(source)

    if declaration is None:
        raise RuntimeError("__EXTERNAL_INTERRUPTS declaration was not found")

    count = int(declaration.group("count"))

    if count != D8_VECTOR_COUNT:
        raise RuntimeError(
            "Unexpected CH32V20x baseline vector count after QingKe fix: "
            f"{count}, expected {D8_VECTOR_COUNT}. Check ch32v20x.yaml first."
        )

    body_start = declaration.end()
    body_end = source.find("\n];", body_start)

    if body_end < 0:
        raise RuntimeError("__EXTERNAL_INTERRUPTS closing ]; was not found")

    body = source[body_start:body_end]
    entries = list(VECTOR_ENTRY_PATTERN.finditer(body))

    if len(entries) != D8_VECTOR_COUNT:
        raise RuntimeError(
            "Could not parse every CH32V20x baseline vector entry: "
            f"parsed {len(entries)}, expected {D8_VECTOR_COUNT}"
        )

    handlers = [entry.group("handler") for entry in entries]
    uart4_index = 66 - EXTERNAL_INTERRUPT_OFFSET
    uart4_handler = handlers[uart4_index]

    if uart4_handler is None or uart4_handler.upper() != "UART4":
        raise RuntimeError(
            "CH32V20x baseline interrupt 66 must be UART4, found "
            f"{uart4_handler!r}"
        )

    for irq in range(61, 70):
        if irq == 66:
            continue
        if handlers[irq - EXTERNAL_INTERRUPT_OFFSET] is None:
            raise RuntimeError(
                f"CH32V20x D8/D8W superset interrupt {irq} is reserved. "
                "The YAML baseline is incomplete."
            )

    d6_handlers = handlers[: 61 - EXTERNAL_INTERRUPT_OFFSET]
    d6_handlers.append(uart4_handler)

    if len(d6_handlers) != D6_VECTOR_COUNT:
        raise RuntimeError(
            f"Internal D6 vector count error: {len(d6_handlers)}"
        )

    cfg_start = source.rfind(
        '#[cfg(feature = "rt")]',
        max(0, declaration.start() - 1024),
        declaration.start(),
    )

    if cfg_start < 0:
        raise RuntimeError(
            'Could not find #[cfg(feature = "rt")] for '
            "__EXTERNAL_INTERRUPTS"
        )

    block_end = body_end + len("\n];")
    prefix = source[cfg_start:declaration.start()]

    d6_prefix = prefix.replace(
        '#[cfg(feature = "rt")]',
        f'#[cfg(all(feature = "rt", feature = "{D6_FEATURE}"))]',
        1,
    )
    d8_prefix = prefix.replace(
        '#[cfg(feature = "rt")]',
        f'#[cfg(all(feature = "rt", feature = "{D8_FEATURE}", '
        f'not(feature = "{D6_FEATURE}")))]',
        1,
    )

    d6_declaration = declaration.group(0).replace(
        f"[Vector; {D8_VECTOR_COUNT}]",
        f"[Vector; {D6_VECTOR_COUNT}]",
    )
    d6_body = "\n" + "\n".join(
        vector_entry(handler) for handler in d6_handlers
    )

    d6_block = d6_prefix + d6_declaration + d6_body + "\n];"
    d8_block = (
        d8_prefix
        + declaration.group(0)
        + body
        + "\n];"
    )

    return (
        source[:cfg_start]
        + d6_block
        + "\n"
        + d8_block
        + source[block_end:]
    )


def vector_entry(handler: str | None) -> str:
    if handler is None:
        return "    Vector { _reserved: 0 },"

    return f"    Vector {{ _handler: {handler} }},"


def split_interrupt_module(source: str) -> str:
    module_match = re.search(
        r"(?m)^#\[doc\(hidden\)\]\s*\npub mod interrupt\s*\{",
        source,
    )

    if module_match is None:
        raise RuntimeError(
            "Generated hidden interrupt module was not found. "
            "The svd2rust interrupt output format may have changed."
        )

    open_brace = source.find("{", module_match.start(), module_match.end())
    module_end = find_matching_brace(source, open_brace) + 1
    module = source[module_match.start():module_end]

    reexport_match = re.search(
        r"(?m)^pub use (?:self::)?interrupt::Interrupt;\s*$",
        source[module_end:],
    )

    if reexport_match is None:
        raise RuntimeError("Generated Interrupt re-export was not found")

    reexport_start = module_end + reexport_match.start()
    reexport_end = module_end + reexport_match.end()

    between = source[module_end:reexport_start]
    if between.strip():
        raise RuntimeError(
            "Unexpected generated code between interrupt module and re-export"
        )

    d6_module = make_d6_interrupt_module(module)
    d8_module = module

    replacement = (
        f'#[cfg(feature = "{D6_FEATURE}")]\n'
        + d6_module
        + "\n"
        + f'#[cfg(all(feature = "{D8_FEATURE}", '
        + f'not(feature = "{D6_FEATURE}")))]\n'
        + d8_module
        + "\n"
        + f'#[cfg(any(feature = "{D6_FEATURE}", feature = "{D8_FEATURE}"))]\n'
        + "pub use self::interrupt::Interrupt;"
    )

    return source[:module_match.start()] + replacement + source[reexport_end:]


def make_d6_interrupt_module(module: str) -> str:
    lines = module.splitlines()
    enum_seen_uart4 = False
    try_from_seen_uart4 = False
    removed_enum_values: set[int] = set()
    removed_try_from_values: set[int] = set()

    output: list[str] = []

    for line in lines:
        variant = INTERRUPT_VARIANT_PATTERN.match(line)

        if variant is not None:
            name = variant.group("name")
            value = int(variant.group("value"))

            if value in D8_ONLY_INTERRUPT_VALUES:
                remove_pending_doc_attributes(output)
                removed_enum_values.add(value)
                continue

            if value == 66 and name.upper() == "UART4":
                update_pending_interrupt_doc(output, 66, 61)
                line = (
                    f'{variant.group("indent")}{name} = 61,'
                )
                enum_seen_uart4 = True

        arm = TRY_FROM_ARM_PATTERN.match(line)

        if arm is not None:
            name = arm.group("name")
            value = int(arm.group("value"))

            if value in D8_ONLY_INTERRUPT_VALUES:
                removed_try_from_values.add(value)
                continue

            if value == 66 and name.upper() == "UART4":
                line = (
                    f'{arm.group("indent")}61 => Ok(Interrupt::{name}),' 
                )
                try_from_seen_uart4 = True

        output.append(line)

    if not enum_seen_uart4:
        raise RuntimeError("Generated Interrupt enum does not contain UART4 = 66")

    if not try_from_seen_uart4:
        raise RuntimeError(
            "Generated Interrupt::try_from does not contain UART4 at value 66"
        )

    if removed_enum_values != D8_ONLY_INTERRUPT_VALUES:
        missing = sorted(D8_ONLY_INTERRUPT_VALUES - removed_enum_values)
        raise RuntimeError(
            "Generated Interrupt enum is missing expected D8-only values: "
            f"{missing}"
        )

    if removed_try_from_values != D8_ONLY_INTERRUPT_VALUES:
        missing = sorted(D8_ONLY_INTERRUPT_VALUES - removed_try_from_values)
        raise RuntimeError(
            "Generated Interrupt::try_from is missing expected D8-only values: "
            f"{missing}"
        )

    return "\n".join(output)


def remove_pending_doc_attributes(lines: list[str]) -> None:
    while lines:
        stripped = lines[-1].lstrip()

        if stripped.startswith("///") or stripped.startswith("#[doc"):
            lines.pop()
            continue

        break


def update_pending_interrupt_doc(
    lines: list[str], old_value: int, new_value: int
) -> None:
    index = len(lines) - 1

    while index >= 0:
        stripped = lines[index].lstrip()

        if not (stripped.startswith("///") or stripped.startswith("#[doc")):
            break

        lines[index] = re.sub(
            rf"(?<!\d){old_value}(?=\s*-)",
            str(new_value),
            lines[index],
            count=1,
        )
        index -= 1


def find_matching_brace(source: str, open_brace: int) -> int:
    depth = 0
    index = open_brace
    state = "normal"

    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if state == "line_comment":
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 1
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == '"':
                state = "normal"
        elif state == "char":
            if char == "\\":
                index += 1
            elif char == "'":
                state = "normal"
        else:
            if char == "/" and next_char == "/":
                state = "line_comment"
                index += 1
            elif char == "/" and next_char == "*":
                state = "block_comment"
                index += 1
            elif char == '"':
                state = "string"
            elif char == "'":
                state = "char"
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index

        index += 1

    raise RuntimeError("Matching closing brace was not found")


def insert_variant_checks(source: str) -> str:
    anchor = source.find('#[cfg(feature = "rt")]')

    if anchor < 0:
        raise RuntimeError(
            'Could not find the generated #[cfg(feature = "rt")] block'
        )

    checks = (
        RUST_MARKER
        + "\n"
        + f'#[cfg(all(feature = "{D6_FEATURE}", feature = "{D8_FEATURE}"))]\n'
        + 'compile_error!("Select only one CH32V20x interrupt layout: '
        + f'{D6_FEATURE} or {D8_FEATURE}.");\n'
        + f'#[cfg(not(any(feature = "{D6_FEATURE}", feature = "{D8_FEATURE}")))]\n'
        + 'compile_error!("Select a CH32V20x interrupt layout: '
        + f'{D6_FEATURE} or {D8_FEATURE}.");\n\n'
    )

    return source[:anchor] + checks + source[anchor:]


def patch_cargo_toml(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    fixed = original

    features_start = fixed.find("[features]")

    if features_start < 0:
        raise RuntimeError("[features] section was not found in Cargo.toml")

    next_section = fixed.find("\n[", features_start + len("[features]"))
    features_end = len(fixed) if next_section < 0 else next_section
    features_block = fixed[features_start:features_end]

    base_pattern = re.compile(
        rf"(?m)^{re.escape(BASE_FEATURE)}\s*=\s*\[\]\s*$"
    )
    base_match = base_pattern.search(features_block)

    if base_match is None:
        raise RuntimeError(
            f"{BASE_FEATURE} = [] was not found in Cargo.toml [features]"
        )

    if D6_FEATURE not in features_block and D8_FEATURE not in features_block:
        insert_at = features_start + base_match.end()
        additions = (
            f'\n{D6_FEATURE} = ["{BASE_FEATURE}"]'
            f'\n{D8_FEATURE} = ["{BASE_FEATURE}"]'
        )
        fixed = fixed[:insert_at] + additions + fixed[insert_at:]
    elif D6_FEATURE not in features_block or D8_FEATURE not in features_block:
        raise RuntimeError(
            "Cargo.toml contains only one CH32V20x variant feature; "
            "refusing to guess how to repair the partial state"
        )

    docs_start = fixed.find("[package.metadata.docs.rs]")

    if docs_start < 0:
        raise RuntimeError(
            "[package.metadata.docs.rs] section was not found in Cargo.toml"
        )

    docs_end = fixed.find("\n[", docs_start + 1)
    if docs_end < 0:
        docs_end = len(fixed)

    docs_block = fixed[docs_start:docs_end]

    if D8_FEATURE not in docs_block:
        if BASE_FEATURE not in docs_block:
            raise RuntimeError(
                "docs.rs feature list does not contain ch32v20x"
            )
        docs_block = docs_block.replace(BASE_FEATURE, D8_FEATURE, 1)
        fixed = fixed[:docs_start] + docs_block + fixed[docs_end:]

    if fixed == original:
        return False

    path.write_text(fixed, encoding="utf-8")
    return True


def patch_readme(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    fixed = original

    base_usage = f'features = ["{BASE_FEATURE}", "critical-section"]'
    d6_usage = f'features = ["{D6_FEATURE}", "critical-section"]'

    if base_usage in fixed:
        fixed = fixed.replace(base_usage, d6_usage, 1)
    elif d6_usage not in fixed:
        raise RuntimeError(
            "README dependency feature example was not found"
        )

    if README_MARKER not in fixed:
        anchor = "\nIn your code:\n"

        if anchor not in fixed:
            raise RuntimeError("README 'In your code:' anchor was not found")

        note = (
            "\n"
            + README_MARKER
            + "\n"
            + f'Use `"{D6_FEATURE}"` for CH32V20x_D6 and '
            + f'`"{D8_FEATURE}"` for CH32V20x_D8/D8W.\n'
        )
        fixed = fixed.replace(anchor, note + anchor, 1)

    if fixed == original:
        return False

    path.write_text(fixed, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add CH32V20x D6/D8 interrupt-layout features to a generated "
            "ch32v2 PAC while keeping one shared peripheral/register module."
        )
    )

    parser.add_argument(
        "crate_dir",
        type=Path,
        help="Path to the generated ch32v2 crate directory",
    )

    args = parser.parse_args()
    crate_dir = args.crate_dir.resolve()

    if not crate_dir.is_dir():
        raise NotADirectoryError(crate_dir)

    fix_ch32v20x_variants(crate_dir)


if __name__ == "__main__":
    main()
