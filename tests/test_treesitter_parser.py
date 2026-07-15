"""Unit tests for treesitter_parser v2 (schema, positions, resolution inputs)."""

from graph_generator.treesitter_parser import (
    ENTITIES_VERSION,
    chunk_by_structure,
    parse_entities,
)


def _first_method(ent, class_name, method_name):
    for cls in ent["classes"]:
        if cls["name"] == class_name:
            for m in cls["methods"]:
                if m["name"] == method_name:
                    return m
    raise AssertionError(f"{class_name}::{method_name} not found")


def test_entities_version_is_3():
    assert ENTITIES_VERSION == 3


def test_unsupported_extension_returns_none():
    assert parse_entities("x.py", "print(1)") is None


def test_basic_class_fields_and_spans():
    src = """<?php
namespace App\\Model;

class User extends Base implements \\JsonSerializable
{
    public function jsonSerialize(): mixed
    {
        return [];
    }
}
"""
    ent = parse_entities("u.php", src)
    cls = ent["classes"][0]
    assert cls["fqcn"] == "App\\Model\\User"
    assert cls["start_line"] == 4 and cls["end_line"] == 10
    assert cls["base_classes"] == ["Base"]
    assert cls["interfaces"] == ["JsonSerializable"]
    relations = {(h["qualified"], h["relation"]) for h in cls["heritage"]}
    assert relations == {("Base", "extends"), ("\\JsonSerializable", "implements")}
    m = cls["methods"][0]
    assert m["member_kind"] == "method"
    assert m["start_line"] == 6 and m["end_line"] == 9


def test_call_sites_kinds_receivers_and_positions():
    src = """<?php
class C {
    public function f($o): void {
        helper();
        $o->run();
        $o?->maybe();
        C::stat();
        $x = new \\App\\Thing();
    }
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "f")
    sites = {(s["name"], s["kind"]) for s in m["call_sites"]}
    assert sites == {
        ("helper", "function"), ("run", "method"), ("maybe", "nullsafe"),
        ("stat", "static"), ("Thing", "new"),
    }
    by_name = {s["name"]: s for s in m["call_sites"]}
    assert by_name["run"]["receiver"] == "$o"
    assert by_name["stat"]["receiver"] == "C"
    assert by_name["Thing"]["qualified"] == "\\App\\Thing"
    # legacy calls list: named function/method/static only, no `new`
    assert m["calls"] == ["helper", "run", "maybe", "stat"]
    # positions point at the name token
    assert by_name["run"]["line"] == 5
    src_line = src.splitlines()[4]
    assert src_line[by_name["run"]["col"]:].startswith("run")


def test_utf16_columns_with_multibyte_prefix():
    src = '<?php\nclass X { function f() { $y = "日本語テキスト"; $this->target(); } }\n'
    ent = parse_entities("x.php", src)
    m = _first_method(ent, "X", "f")
    t = [s for s in m["call_sites"] if s["name"] == "target"][0]
    line = src.splitlines()[1]
    # Python str indexes are UTF-16-equivalent here (all BMP chars)
    assert t["col"] == line.index("target")
    assert t["col"] != len(line[: line.index("target")].encode("utf-8"))


def test_dynamic_call_sites_flagged():
    src = """<?php
class C {
    public function f($fn): void {
        $fn();
        $this->$fn();
        C::$fn();
    }
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "f")
    dyn = [s for s in m["call_sites"] if s["dynamic"]]
    assert len(dyn) == 3
    assert all(s["name"] == "" for s in dyn)
    assert m["calls"] == []


def test_str_args_literals_and_non_literals():
    src = """<?php
class C {
    public function f(): void {
        $this->fetchTable('Billing.Audits');
        $this->load("Users", $x, 'third', 'fourth');
        $this->skip("interp {$x}");
    }
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "f")
    by_name = {s["name"]: s for s in m["call_sites"]}
    assert by_name["fetchTable"]["str_args"] == ["Billing.Audits"]
    assert by_name["load"]["str_args"] == ["Users", None, "third"]  # capped at 3
    assert by_name["skip"]["str_args"] == [None]  # interpolation is not literal


def test_assignments_new_call_prop_var():
    src = """<?php
class C {
    public function f(): void {
        $a = new \\Acme\\Report();
        $b = $this->fetchTable('Users');
        $c = $this->Users;
        $d = $c;
    }
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "f")
    kinds = {a["var"]: a["rhs_kind"] for a in m["assignments"]}
    assert kinds == {"$a": "new", "$b": "call", "$c": "prop", "$d": "var"}
    a = [x for x in m["assignments"] if x["var"] == "$a"][0]
    assert a["qualified"] == "\\Acme\\Report" and a["name"] == "Report"
    b = [x for x in m["assignments"] if x["var"] == "$b"][0]
    # joins to the call_site at the same position
    site = [s for s in m["call_sites"] if s["name"] == "fetchTable"][0]
    assert (b["line"], b["col"]) == (site["line"], site["col"])


def test_prop_sites_and_class_refs():
    src = """<?php
class C {
    public function f(): void {
        $n = $this->user->name;
        $cls = \\App\\Model\\User::class;
        $cb = [$this, 'f'];
    }
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "f")
    props = {(p["receiver"], p["name"]) for p in m["prop_sites"]}
    assert ("$this", "user") in props
    assert ("$this->user", "name") in props
    refs = {(r["kind"], r.get("text", "")) for r in m["class_refs"]}
    assert ("class_literal", "\\App\\Model\\User") in refs
    ac = [r for r in m["class_refs"] if r["kind"] == "array_callable"][0]
    assert ac["name"] == "f" and ac["text"] == "$this"


def test_group_use_prefix_and_alias():
    src = "<?php\nuse Cake\\ORM\\{Table, Query as Q};\nuse Foo\\Bar as Baz;\nuse function Foo\\helper;\nuse const Foo\\LIMIT;\n"
    ent = parse_entities("u.php", src)
    uses = {u["fqcn"]: u for u in ent["uses"]}
    assert uses["Cake\\ORM\\Table"]["alias"] == "Table"
    assert uses["Cake\\ORM\\Query"]["alias"] == "Q"
    assert uses["Foo\\Bar"]["alias"] == "Baz"
    assert uses["Foo\\helper"]["kind"] == "function"
    assert uses["Foo\\LIMIT"]["kind"] == "const"


def test_member_kinds_property_enum_trait_global():
    src = """<?php
namespace App;
enum Suit: string { case Hearts = 'H'; }
trait T { public function tm(): void {} }
class Y {
    use T;
    private string $note;
    public function m(): void {}
}
function topLevel(): void {}
"""
    ent = parse_entities("y.php", src)
    by_name = {c["name"]: c for c in ent["classes"]}
    assert by_name["Suit"]["kind"] == "enum"
    assert by_name["Suit"]["methods"][0]["member_kind"] == "enum_case"
    assert by_name["T"]["kind"] == "trait"
    y = by_name["Y"]
    kinds = {m["name"]: m["member_kind"] for m in y["methods"]}
    assert kinds == {"m": "method", "note": "property"}
    assert ("T", "uses") in {(h["qualified"], h["relation"]) for h in y["heritage"]}
    g = by_name["(global)"]
    assert g["fqcn"] == "" and g["methods"][0]["member_kind"] == "function"


def test_property_name_collision_with_method_is_dropped():
    src = """<?php
class C {
    public $save;
    public function save(): void {}
}
"""
    ent = parse_entities("c.php", src)
    members = [m for m in ent["classes"][0]["methods"] if m["name"] == "save"]
    assert len(members) == 1
    assert members[0]["member_kind"] == "method"


def test_param_types_including_promoted():
    src = """<?php
class C {
    public function __construct(private \\App\\Svc $svc, ?int $n = null, $untyped) {}
}
"""
    ent = parse_entities("c.php", src)
    m = _first_method(ent, "C", "__construct")
    assert m["param_types"] == {"$svc": "\\App\\Svc", "$n": "?int"}


def test_ctp_html_mixed_template_parses():
    src = "<html><body><?php echo $this->Html->link('go', '/'); ?></body></html>"
    ent = parse_entities("view.ctp", src)
    assert ent is not None
    assert ent["classes"] == []  # no declarations, but parse succeeds


def test_chunking_unchanged_smoke():
    src = "<?php\nclass A { public function f(): void {} }\n"
    chunks = chunk_by_structure("a.php", src)
    assert chunks and "class A" in chunks[0]
