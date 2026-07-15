"""Regression tests for the cross-repo accuracy review findings.

Each test is the executable form of a confirmed defect repro: PHP name
resolution (no global fallback for classes, FQ-verbatim callable strings,
ns→global fallback for functions), ownership rules (vendor mirrors defer,
committed local forks win, multi-owner ambiguity reported), DI pairs wired in
a third repo, plural addArguments, id_prefix enforcement, and the drop report.
"""

import json

from graph_generator import config, crossref
from graph_generator.pipeline import ID_SCHEME
from graph_generator.treesitter_parser import ENTITIES_VERSION
from tests.test_crossref import _ingest


def _derive(repos):
    reg = crossref.build_registry(repos)
    return crossref.derive_cross_repo(repos, reg, printer=lambda *a: None)


# ── F1: PHP class-name resolution — never fall back to global ──────────

def test_unqualified_class_name_never_falls_back_to_global(tmp_path):
    """`Widget::class` inside `namespace App` means App\\Widget, period — it
    must NOT edge to another repo's global Widget."""
    legacy = _ingest(tmp_path, "legacy", {
        "src/Widget.php": "<?php\nclass Widget {}\n"})
    app = _ingest(tmp_path, "app", {
        "src/Widget.php": "<?php\nnamespace App;\nclass Widget {}\n",
        "src/UsesWidget.php": (
            "<?php\nnamespace App;\nclass UsesWidget {\n"
            "    public function f(): string { return Widget::class; }\n}\n"),
    })
    derived = _derive([legacy, app])
    assert all(r[5] != "Widget" for r in derived["CrossRepoRef"])
    assert not derived["CrossRepoRef"]  # App\Widget is local → no edge at all


def test_callable_string_is_fully_qualified_verbatim(tmp_path):
    """PHP callable strings ignore `use` aliases: with `use Shared\\Money\\Price
    as P;`, the string 'P::add' names a global class P (unowned), never the
    aliased Price. The FQ string 'Shared\\Money\\Price::add' does edge."""
    shared = _ingest(tmp_path, "shared", {
        "src/Money/Price.php": (
            "<?php\nnamespace Shared\\Money;\n"
            "class Price { public function add(): void {} }\n")})
    web = _ingest(tmp_path, "web", {
        "src/Hooks.php": (
            "<?php\nnamespace Web;\nuse Shared\\Money\\Price as P;\n"
            "class Hooks {\n"
            "    public function alias(): string { return 'P::add'; }\n"
            "    public function fq(): string { return 'Shared\\\\Money\\\\Price::add'; }\n"
            "}\n"),
    })
    derived = _derive([shared, web])
    kinds = {(r[5], r[6]) for r in derived["CrossRepoRef"]}
    assert ("Shared\\Money\\Price", "class_ref") in kinds
    # the alias string produced no edge and the unowned 'P' was sampled
    assert all(r[5] != "P" for r in derived["CrossRepoRef"])
    assert "P" in derived["drops"]["unowned_sample"]


# ── F2: ownership — local forks win, vendor mirrors defer ──────────────

_PRICE = ("<?php\nnamespace Shared\\Money;\n"
          "class Price { public function add(): void {} }\n")
_CONSUMER_SRC = ("<?php\nnamespace Consumer;\nuse Shared\\Money\\Price;\n"
                 "class Order { public function t(Price $p): void { $p->add(); } }\n")


def test_committed_local_fork_wins_and_is_counted(tmp_path):
    consumer = _ingest(tmp_path, "consumer", {
        "lib/Money/Price.php": _PRICE,     # committed app-origin fork
        "src/Order.php": _CONSUMER_SRC,
    })
    shared = _ingest(tmp_path, "shared", {"src/Money/Price.php": _PRICE})
    derived = _derive([shared, consumer])
    assert not [r for r in derived["CrossRepoRef"] + derived["CrossRepoCalls"]
                if r[3] == "consumer"]
    assert derived["stats"]["local_definition"] > 0


def test_vendor_copy_defers_to_source_repo(tmp_path):
    """A consumer ingested with --include-vendor carries a vendor/ mirror of
    the shared lib: the mirror owns nothing, so refs edge to the source repo."""
    shared = _ingest(tmp_path, "shared", {"src/Money/Price.php": _PRICE})
    web = _ingest(tmp_path, "web", {
        "src/Order.php": _CONSUMER_SRC.replace("Consumer", "Web"),
        "vendor/acme/shared/src/Money/Price.php": _PRICE,
    })
    api = _ingest(tmp_path, "api", {
        "src/Order.php": _CONSUMER_SRC.replace("Consumer", "Api"),
        "vendor/acme/shared/src/Money/Price.php": _PRICE,
    })
    derived = _derive([shared, web, api])
    pairs = {(r[3], r[4]) for r in derived["CrossRepoRef"]}
    assert pairs == {("web", "shared"), ("api", "shared")}
    call_pairs = {(c[3], c[4]) for c in derived["CrossRepoCalls"]}
    assert call_pairs == {("web", "shared"), ("api", "shared")}
    assert derived["stats"]["ambiguous_targets"] == 0


def test_vendor_files_never_act_as_reference_sources(tmp_path):
    """References made INSIDE a repo's vendor/ tree are mirror-internal noise
    and must not create cross-repo edges."""
    shared = _ingest(tmp_path, "shared", {"src/Money/Price.php": _PRICE})
    web = _ingest(tmp_path, "web", {
        "vendor/acme/glue/src/Glue.php": (
            "<?php\nnamespace Acme\\Glue;\nuse Shared\\Money\\Price;\n"
            "class Glue { public function g(Price $p): void { $p->add(); } }\n"),
    })
    derived = _derive([shared, web])
    assert not derived["CrossRepoRef"] and not derived["CrossRepoCalls"]


def test_two_foreign_owners_is_ambiguous_and_reported(tmp_path):
    a = _ingest(tmp_path, "a", {"src/Money/Price.php": _PRICE})
    b = _ingest(tmp_path, "b", {"src/Money/Price.php": _PRICE})
    consumer = _ingest(tmp_path, "consumer", {"src/Order.php": _CONSUMER_SRC})
    derived = _derive([a, b, consumer])
    assert not derived["CrossRepoRef"] and not derived["CrossRepoCalls"]
    assert derived["drops"]["ambiguous"] == {"Shared\\Money\\Price": ["a", "b"]}


# ── F4: id_prefix enforcement in discover_repos ─────────────────────────

def test_discover_repos_skips_mismatched_id_prefix(tmp_path):
    out = tmp_path / "out"
    for repo, prefix in (("good", config.ID_PREFIX), ("stale", "other_prefix")):
        d = out / "repos" / repo
        d.mkdir(parents=True)
        (d / "graph_meta.json").write_text(json.dumps(
            {"repo": repo, "target_dir": f"/src/{repo}",
             "id_scheme": ID_SCHEME, "id_prefix": prefix}))
        (d / "entities.json").write_text(json.dumps(
            {"version": ENTITIES_VERSION, "entities": {}}))
    warnings = []
    repos = crossref.discover_repos(str(out), printer=warnings.append)
    assert [r["repo"] for r in repos] == ["good"]
    assert any("ID_PREFIX" in w for w in warnings)


# ── F5: heritage kinds (extends / trait uses) ───────────────────────────

def test_extends_and_trait_use_become_edges(tmp_path):
    lib = _ingest(tmp_path, "lib", {
        "src/Base.php": "<?php\nnamespace Lib\\Core;\nclass Base {}\n",
        "src/Helper.php": "<?php\nnamespace Lib\\Core;\ntrait Helper {}\n",
    })
    app = _ingest(tmp_path, "app", {
        "src/Child.php": (
            "<?php\nnamespace App;\n"
            "class Child extends \\Lib\\Core\\Base { use \\Lib\\Core\\Helper; }\n"),
    })
    derived = _derive([lib, app])
    got = {(r[5], r[6]) for r in derived["CrossRepoRef"]}
    assert got == {("Lib\\Core\\Base", "extends"), ("Lib\\Core\\Helper", "uses")}


# ── F7 / F9: DI pairs across three repos + plural addArguments ─────────

_WIRING = (
    "<?php\nnamespace Infra;\n"
    "class Application {\n"
    "    public function services(ContainerInterface $container): void {\n"
    "        $container->add(\\LibA\\Contracts\\I::class, \\LibB\\Impl\\C::class);\n"
    "        $container->add(Svc::class)\n"
    "            ->addArguments([\\LibA\\Contracts\\I::class, \\LibB\\Impl\\C::class]);\n"
    "        $container->add(LocalI::class, LocalC::class);\n"
    "    }\n"
    "}\n")

_INFRA_LOCALS = (
    "<?php\nnamespace Infra;\n"
    "interface LocalI {}\n")


def _three_repo_di(tmp_path):
    liba = _ingest(tmp_path, "liba", {
        "src/I.php": "<?php\nnamespace LibA\\Contracts;\ninterface I {}\n"})
    libb = _ingest(tmp_path, "libb", {
        "src/C.php": "<?php\nnamespace LibB\\Impl;\nclass C {}\n"})
    infra = _ingest(tmp_path, "infra", {
        "src/Application.php": _WIRING,
        "src/LocalI.php": _INFRA_LOCALS,
        "src/LocalC.php": "<?php\nnamespace Infra;\nclass LocalC implements LocalI {}\n",
        "src/Svc.php": "<?php\nnamespace Infra;\nclass Svc {}\n",
    })
    return _derive([liba, libb, infra])


def test_di_pair_wired_in_third_repo_gets_edge(tmp_path):
    """add(I::class, C::class) where I lives in liba and C in libb — the
    wiring repo owns neither endpoint, but the bind is real coupling."""
    derived = _three_repo_di(tmp_path)
    binds = {(b[3], b[4], b[5]) for b in derived["DiBinds"]}
    assert ("liba", "libb", "LibB\\Impl\\C") in binds


def test_di_both_endpoints_local_stays_with_per_repo_pass(tmp_path):
    derived = _three_repo_di(tmp_path)
    assert all(not (b[3] == "infra" and b[4] == "infra")
               for b in derived["DiBinds"])


def test_di_addarguments_plural_array_form(tmp_path):
    derived = _three_repo_di(tmp_path)
    injects = {(i[3], i[4], i[5]) for i in derived["DiInjects"]}
    assert ("infra", "liba", "LibA\\Contracts\\I") in injects
    assert ("infra", "libb", "LibB\\Impl\\C") in injects


# ── F8: function calls — PHP ns→global fallback, local wins ────────────

def test_global_function_call_falls_back_and_edges(tmp_path):
    libf = _ingest(tmp_path, "libf", {
        "src/functions.php": "<?php\nfunction format_it(int $v): string { return ''; }\n"})
    user = _ingest(tmp_path, "user", {
        "src/App.php": (
            "<?php\nnamespace User\\App;\n"
            "class App { public function run(): void { format_it(1); } }\n")})
    derived = _derive([libf, user])
    assert [(c[3], c[4], c[5]) for c in derived["CrossRepoCalls"]] \
        == [("user", "libf", "format_it")]


def test_local_namespaced_function_shadows_foreign_global(tmp_path):
    libf = _ingest(tmp_path, "libf", {
        "src/functions.php": "<?php\nfunction format_it(int $v): string { return ''; }\n"})
    user = _ingest(tmp_path, "user2", {
        "src/functions.php": (
            "<?php\nnamespace User2\\App;\n"
            "function format_it(int $v): string { return 'local'; }\n"),
        "src/App.php": (
            "<?php\nnamespace User2\\App;\n"
            "class App { public function run(): void { format_it(1); } }\n")})
    derived = _derive([libf, user])
    assert not derived["CrossRepoCalls"]


# ── F6: drops are loud — report formatting + persisted report ───────────

def test_format_drops_mentions_every_category():
    drops = {"ambiguous": {"Shared\\Money\\Price": ["a", "b"]},
             "unowned_sample": ["Cake\\ORM\\Table", "Cake\\ORM\\Query"]}
    stats = {"unowned_refs": 7, "di_unowned": 1, "di_ambiguous": 2,
             "local_definition": 3}
    out = crossref.format_drops(drops, stats)
    assert "AMBIGUOUS" in out and "Shared\\Money\\Price" in out
    assert "defined in ['a', 'b']" in out
    assert "UNOWNED references: 7" in out and "Cake\\ORM" in out
    assert "1 unowned, 2 ambiguous" in out
    assert "own committed code: 3" in out
    assert "nothing dropped" in crossref.format_drops(
        {"ambiguous": {}, "unowned_sample": []}, {})


def test_run_crossref_writes_report_json(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    (tmp_path / "out").mkdir()
    a = _ingest(tmp_path, "a", {"src/Money/Price.php": _PRICE})
    b = _ingest(tmp_path, "b", {"src/Money/Price.php": _PRICE})
    consumer = _ingest(tmp_path, "consumer", {"src/Order.php": _CONSUMER_SRC})
    monkeypatch.setattr(crossref, "discover_repos",
                        lambda *args, **kw: [a, b, consumer])
    lines = []
    derived = crossref.run_crossref(printer=lines.append, write=False)
    report = json.loads((tmp_path / "out" / "crossref_report.json").read_text())
    assert report["drops"]["ambiguous"] == {"Shared\\Money\\Price": ["a", "b"]}
    assert report["stats"] == derived["stats"]
    joined = "\n".join(str(x) for x in lines)
    assert "dropped / unresolved" in joined and "AMBIGUOUS" in joined
