"""Structural chunking (chunk_by_structure) + parser edge cases."""

from graph_generator.treesitter_parser import chunk_by_structure, parse_entities


def test_chunk_unsupported_extension():
    assert chunk_by_structure("x.py", "print(1)") is None


def test_chunk_small_file_single_chunk():
    src = "<?php\nclass A { public function f(): void {} }\n"
    chunks = chunk_by_structure("a.php", src)
    assert chunks and len(chunks) == 1 and "class A" in chunks[0]


def test_chunk_use_grouping_and_multiple_classes():
    src = "<?php\nuse A\\B;\nuse C\\D;\n" + "\n".join(
        f"class K{i} {{ public function m{i}() {{}} }}" for i in range(5))
    chunks = chunk_by_structure("m.php", src, max_chars=200)
    assert len(chunks) >= 1
    joined = "\n".join(chunks)
    assert "K0" in joined and "K4" in joined


def test_chunk_large_class_split_into_members():
    body = "\n".join(
        f"    public function method{i}(): string {{ return '{'x' * 60}'; }}"
        for i in range(40))
    src = f"<?php\nclass Big {{\n{body}\n}}\n"
    chunks = chunk_by_structure("big.php", src, max_chars=500)
    # class exceeds budget → broken into member-level units with class context
    assert len(chunks) > 1
    assert any("class Big" in c for c in chunks)


def test_chunk_oversized_single_member_char_split():
    huge = "x" * 5000
    src = f"<?php\nclass C {{\n    public function f(): string {{ return '{huge}'; }}\n}}\n"
    chunks = chunk_by_structure("c.php", src, max_chars=800)
    assert len(chunks) > 1


def test_chunk_oversized_toplevel_node():
    src = "<?php\n// " + ("y" * 5000) + "\n$x = 1;\n"
    chunks = chunk_by_structure("t.php", src, max_chars=800)
    assert chunks is not None


def test_parse_include_require_targets():
    src = "<?php\nrequire 'bootstrap.php';\ninclude __DIR__ . '/app.php';\n"
    ent = parse_entities("i.php", src)
    assert "bootstrap.php" in ent["imports"]


def test_parse_braced_namespace():
    src = "<?php\nnamespace App { class A { function f(){} } }\n"
    ent = parse_entities("b.php", src)
    assert ent["namespace"] == "App"
    assert ent["classes"][0]["fqcn"] == "App\\A"


def test_parse_empty_and_whitespace():
    assert parse_entities("e.php", "") is not None
    assert parse_entities("e.php", "<?php\n")["classes"] == []
