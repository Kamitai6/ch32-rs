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

D8_ONLY_INTERRUPT_VALUES = {61, 62, 65, 67, 68, 69}
D8_RESERVED_INTERRUPT_VALUES = {63, 64}

# Chapter 27.2 marks these Ethernet reset values as X/XXXX/XXXXXXXX.
# The vendor SVD has a device-wide default resetValue=0, so merely omitting a
# register-level resetValue still makes svd2rust generate Resettable with zero.
# Remove only those inherited reset implementations from the generated PAC.
ETHERNET_UNKNOWN_RESET_SPECS = (
    "ETH_TX_SPEC",
    "ETXST_SPEC",
    "ETXLN_SPEC",
    "ETH_TIM_SPEC",
    "EPAUS_SPEC",
    "MAADRL_SPEC",
    "MAADRH_SPEC",
)

VECTOR_DECLARATION_PATTERN = re.compile(
    r"pub\s+static\s+__EXTERNAL_INTERRUPTS\s*:\s*"
    r"\[\s*Vector\s*;\s*(?P<count>\d+)\s*\]\s*=\s*\[",
    re.DOTALL,
)
VECTOR_ENTRY_PATTERN = re.compile(
    r"Vector\s*\{\s*"
    r"(?:(?:_handler\s*:\s*(?P<handler>[A-Za-z_][A-Za-z0-9_]*))|"
    r"(?:_reserved\s*:\s*(?P<reserved>\d+)))"
    r"\s*,?\s*\}\s*,",
    re.DOTALL,
)
VECTOR_ARRAY_END_PATTERN = re.compile(r"\]\s*;")
CFG_RT_PATTERN = re.compile(
    r'#\[\s*cfg\s*\(\s*feature\s*=\s*"rt"\s*\)\s*\]'
)
LINK_SECTION_PATTERN = re.compile(
    rf'#\[\s*link_section\s*=\s*"{re.escape(LINK_SECTION)}"\s*\]'
)
HIDDEN_INTERRUPT_MODULE_PATTERN = re.compile(
    r"#\[\s*doc\s*\(\s*hidden\s*\)\s*\]\s*"
    r"pub\s+mod\s+interrupt\s*\{",
    re.DOTALL,
)
INTERRUPT_REEXPORT_PATTERN = re.compile(
    r"pub\s+use\s+(?:self\s*::\s*)?interrupt\s*::\s*Interrupt\s*;"
)
INTERRUPT_ENUM_PATTERN = re.compile(
    r"pub\s+enum\s+Interrupt\s*\{",
    re.DOTALL,
)
INTERRUPT_ENUM_ENTRY_PATTERN = re.compile(
    r"(?P<doc>"
    r"#\[\s*doc\s*=\s*\"(?:\\.|[^\"\\])*\"\s*\]\s*"
    r")?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<value>\d+)\s*,",
    re.DOTALL,
)
TRY_FROM_ARM_PATTERN = re.compile(
    r"(?P<value>\d+)\s*=>\s*Ok\s*\(\s*"
    r"Interrupt\s*::\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*,",
    re.DOTALL,
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
    fixed = remove_unknown_ethernet_reset_impls(original)

    # Variant splitting is idempotent and is marked explicitly.  Ethernet
    # reset cleanup must still be allowed on an already variant-patched file
    # so older generated output can be repaired by rerunning this script.
    if RUST_MARKER in fixed:
        if fixed == original:
            return False
        path.write_text(fixed, encoding="utf-8")
        return True

    if LINK_SECTION_PATTERN.search(fixed) is None:
        raise RuntimeError(
            "QingKe vector correction has not been applied yet. Run "
            "fix_qingke_interrupt_vectors.py before this script."
        )

    fixed = split_vector_table(fixed)
    fixed = split_interrupt_module(fixed)
    fixed = insert_variant_checks(fixed)

    if fixed == original:
        return False

    path.write_text(fixed, encoding="utf-8")
    return True



def remove_unknown_ethernet_reset_impls(source: str) -> str:
    fixed = source

    for spec in ETHERNET_UNKNOWN_RESET_SPECS:
        impl_pattern = re.compile(
            rf"impl\s+crate\s*::\s*Resettable\s+for\s+{re.escape(spec)}\s*"
            rf"\{{(?P<body>[^{{}}]*)\}}",
            re.DOTALL,
        )
        match = impl_pattern.search(fixed)

        if match is None:
            # Already fixed is valid.  If the spec itself exists, lack of a
            # Resettable impl is exactly the desired state.
            if re.search(rf"pub\s+struct\s+{re.escape(spec)}\s*;", fixed):
                continue
            raise RuntimeError(
                f"Ethernet register spec {spec} was not found in generated PAC"
            )

        body = match.group("body").strip()
        if body:
            zero_reset_pattern = re.compile(
                r"const\s+RESET_VALUE\s*:\s*[A-Za-z0-9_:<>]+\s*=\s*"
                r"(?:0x0+|0+)\s*;\s*",
                re.DOTALL,
            )
            if zero_reset_pattern.fullmatch(body) is None:
                raise RuntimeError(
                    f"Refusing to remove non-zero or unexpected reset body for {spec}: "
                    f"{body!r}"
                )

        # svd2rust emits one doc attribute immediately before Resettable.
        # Remove it as well so it cannot become attached to the next Rust item.
        doc_start = find_generated_reset_doc_start(fixed, match.start(), spec)
        fixed = fixed[:doc_start] + fixed[match.end():]

    return fixed


def find_generated_reset_doc_start(source: str, impl_start: int, spec: str) -> int:
    register_name = spec.removesuffix("_SPEC")
    prefix = source[:impl_start]
    doc_pattern = re.compile(
        rf"#\s*\[\s*doc\s*=\s*\"[^\"\n]*`reset\(\)`\s+method\s+sets\s+"
        rf"{re.escape(register_name)}\s+to\s+value\s+[^\"\n]*\"\s*\]\s*$",
        re.DOTALL,
    )
    match = doc_pattern.search(prefix)
    return match.start() if match is not None else impl_start


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
    body_end, array_end = find_vector_array_end(source, body_start)
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

    for irq in D8_ONLY_INTERRUPT_VALUES:
        if handlers[irq - EXTERNAL_INTERRUPT_OFFSET] is None:
            raise RuntimeError(
                f"CH32V20x D8 interrupt {irq} is reserved. "
                "The YAML baseline is incomplete."
            )

    for irq in D8_RESERVED_INTERRUPT_VALUES:
        if handlers[irq - EXTERNAL_INTERRUPT_OFFSET] is not None:
            raise RuntimeError(
                f"CH32V20x D8 interrupt {irq} must be reserved, found "
                f"{handlers[irq - EXTERNAL_INTERRUPT_OFFSET]!r}."
            )

    d6_handlers = handlers[: 61 - EXTERNAL_INTERRUPT_OFFSET]
    d6_handlers.append(uart4_handler)

    if len(d6_handlers) != D6_VECTOR_COUNT:
        raise RuntimeError(
            f"Internal D6 vector count error: {len(d6_handlers)}"
        )

    cfg_matches = list(
        CFG_RT_PATTERN.finditer(
            source,
            max(0, declaration.start() - 2048),
            declaration.start(),
        )
    )

    if not cfg_matches:
        raise RuntimeError(
            'Could not find #[cfg(feature = "rt")] for '
            "__EXTERNAL_INTERRUPTS"
        )

    cfg_match = cfg_matches[-1]
    cfg_start = cfg_match.start()
    prefix = source[cfg_start:declaration.start()]

    d6_prefix = CFG_RT_PATTERN.sub(
        f'#[cfg(all(feature = "rt", feature = "{D6_FEATURE}"))]',
        prefix,
        count=1,
    )
    d8_prefix = CFG_RT_PATTERN.sub(
        f'#[cfg(all(feature = "rt", feature = "{D8_FEATURE}", '
        f'not(feature = "{D6_FEATURE}")))]',
        prefix,
        count=1,
    )

    d6_declaration = replace_named_group(
        declaration, "count", str(D6_VECTOR_COUNT)
    )
    d6_body = "\n" + "\n".join(
        vector_entry(handler) for handler in d6_handlers
    ) + "\n"

    d6_block = d6_prefix + d6_declaration + d6_body + "];"
    d8_block = d8_prefix + declaration.group(0) + body + "];"

    return (
        source[:cfg_start]
        + d6_block
        + "\n"
        + d8_block
        + source[array_end:]
    )


def vector_entry(handler: str | None) -> str:
    if handler is None:
        return "    Vector { _reserved: 0 },"

    return f"    Vector {{ _handler: {handler} }},"


def split_interrupt_module(source: str) -> str:
    module_match = HIDDEN_INTERRUPT_MODULE_PATTERN.search(source)

    if module_match is None:
        raise RuntimeError(
            "Generated hidden interrupt module was not found. "
            "The svd2rust interrupt output format may have changed."
        )

    open_brace = source.rfind("{", module_match.start(), module_match.end())
    module_end = find_matching_brace(source, open_brace) + 1
    module = source[module_match.start():module_end]

    reexport_match = INTERRUPT_REEXPORT_PATTERN.search(source, module_end)

    if reexport_match is None:
        raise RuntimeError("Generated Interrupt re-export was not found")

    between = source[module_end:reexport_match.start()]
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

    return (
        source[:module_match.start()]
        + replacement
        + source[reexport_match.end():]
    )


def make_d6_interrupt_module(module: str) -> str:
    enum_match = INTERRUPT_ENUM_PATTERN.search(module)

    if enum_match is None:
        raise RuntimeError("Generated Interrupt enum was not found")

    enum_open = module.rfind("{", enum_match.start(), enum_match.end())
    enum_close = find_matching_brace(module, enum_open)
    enum_body = module[enum_open + 1:enum_close]

    enum_entries = list(INTERRUPT_ENUM_ENTRY_PATTERN.finditer(enum_body))
    enum_values = {int(entry.group("value")) for entry in enum_entries}

    missing_enum_values = sorted(D8_ONLY_INTERRUPT_VALUES - enum_values)
    if missing_enum_values:
        raise RuntimeError(
            "Generated Interrupt enum is missing expected D8-only values: "
            f"{missing_enum_values}"
        )

    uart4_entries = [
        entry
        for entry in enum_entries
        if int(entry.group("value")) == 66
        and entry.group("name").upper() == "UART4"
    ]

    if len(uart4_entries) != 1:
        raise RuntimeError(
            "Generated Interrupt enum does not contain exactly one UART4 = 66"
        )

    new_enum_body_parts: list[str] = []
    cursor = 0

    for entry in enum_entries:
        new_enum_body_parts.append(enum_body[cursor:entry.start()])
        name = entry.group("name")
        value = int(entry.group("value"))

        if value in D8_ONLY_INTERRUPT_VALUES:
            cursor = entry.end()
            continue

        entry_text = entry.group(0)

        if value == 66 and name.upper() == "UART4":
            entry_text = replace_named_group(entry, "value", "61")
            entry_text = re.sub(
                r"(?<!\d)66(?=\s*-)",
                "61",
                entry_text,
                count=1,
            )

        new_enum_body_parts.append(entry_text)
        cursor = entry.end()

    new_enum_body_parts.append(enum_body[cursor:])
    new_enum_body = "".join(new_enum_body_parts)
    fixed = (
        module[:enum_open + 1]
        + new_enum_body
        + module[enum_close:]
    )

    removed_try_from_values: set[int] = set()
    try_from_seen_uart4 = False

    def patch_try_from_arm(match: re.Match[str]) -> str:
        nonlocal try_from_seen_uart4

        name = match.group("name")
        value = int(match.group("value"))

        if value in D8_ONLY_INTERRUPT_VALUES:
            removed_try_from_values.add(value)
            return ""

        if value == 66 and name.upper() == "UART4":
            try_from_seen_uart4 = True
            return "61 => Ok(Interrupt::UART4),"

        return match.group(0)

    fixed = TRY_FROM_ARM_PATTERN.sub(patch_try_from_arm, fixed)

    if removed_try_from_values != D8_ONLY_INTERRUPT_VALUES:
        missing = sorted(D8_ONLY_INTERRUPT_VALUES - removed_try_from_values)
        raise RuntimeError(
            "Generated Interrupt::try_from is missing expected D8-only values: "
            f"{missing}"
        )

    if not try_from_seen_uart4:
        raise RuntimeError(
            "Generated Interrupt::try_from does not contain UART4 at value 66"
        )

    return fixed


def find_vector_array_end(source: str, body_start: int) -> tuple[int, int]:
    closing = VECTOR_ARRAY_END_PATTERN.search(source, body_start)

    if closing is None:
        raise RuntimeError("__EXTERNAL_INTERRUPTS closing ]; was not found")

    return closing.start(), closing.end()


def replace_named_group(
    match: re.Match[str], group_name: str, replacement: str
) -> str:
    text = match.group(0)
    start = match.start(group_name) - match.start()
    end = match.end(group_name) - match.start()
    return text[:start] + replacement + text[end:]


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
    anchor_match = CFG_RT_PATTERN.search(source)

    if anchor_match is None:
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

    return source[:anchor_match.start()] + checks + source[anchor_match.start():]


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
            + f'`"{D8_FEATURE}"` for CH32V20x_D8.\n'
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
