"""CakePHP string-convention mappings (pure functions, no I/O).

These translate framework string conventions into candidate FQCNs —
`fetchTable('Billing.Audits')` → `Billing\\Model\\Table\\AuditsTable` — for the
resolution phase to confirm against real, existing classes. Emitting a
candidate here never creates an edge by itself: resolution.py only records a
target after verifying the class/member actually exists.
"""

from __future__ import annotations

import re

# Loader-method name (lowercased) → (app/plugin sub-namespace, class suffix).
CAKE_LOADERS: dict[str, tuple[str, str]] = {
    "fetchtable": ("Model\\Table", "Table"),
    "loadmodel": ("Model\\Table", "Table"),
    "addbehavior": ("Model\\Behavior", "Behavior"),
    "loadcomponent": ("Controller\\Component", "Component"),
    "loadhelper": ("View\\Helper", "Helper"),
    "addhelper": ("View\\Helper", "Helper"),
}

# CakePHP core puts these mixins under different sub-namespaces than app/plugin
# code (e.g. `Cake\ORM\Behavior\TimestampBehavior`, not `Cake\Model\Behavior`).
CAKE_CORE_SUBNS: dict[str, str] = {
    "addbehavior": "ORM\\Behavior",
    "loadcomponent": "Controller\\Component",
    "loadhelper": "View\\Helper",
    "addhelper": "View\\Helper",
}

# Receiver texts that mark a table-locator `get('Users')` call.
_LOCATOR_RECEIVER = re.compile(
    r"(getTableLocator\(\)|TableRegistry|tableLocator)", re.IGNORECASE)

# 'Acme\Reporting\Report::run' / '\Acme\X::run' literal callables
CALLABLE_LITERAL = re.compile(
    r"^\\?[A-Za-z_][\w\\]*::[A-Za-z_]\w*$")

# Cake magic finder prefixes handled by Table::__call
MAGIC_FINDER = re.compile(r"^find(?:All)?By[A-Z]\w*$")


def split_plugin(arg: str) -> tuple[str | None, str]:
    """'Billing.Audits' → ('Billing', 'Audits'); 'Users' → (None, 'Users')."""
    if "." in arg:
        plugin, name = arg.split(".", 1)
        if plugin and name:
            return plugin, name
    return None, arg


def singularize(word: str) -> str:
    """Minimal CakePHP-style Inflector::singularize for entity names.

    Wrong guesses are harmless — every candidate is existence-checked.
    """
    lw = word.lower()
    if lw.endswith("ies") and len(word) > 3:
        return word[:-3] + ("Y" if word[-3].isupper() else "y")
    for suf in ("sses", "xes", "ches", "shes"):
        if lw.endswith(suf):
            return word[:-2]
    if lw.endswith("s") and not lw.endswith("ss"):
        return word[:-1]
    return word


def tableize(class_simple: str) -> str:
    """CakePHP table-name convention: `UsersTable` → `users`,
    `ArticleCategoriesTable` → `article_categories`."""
    base = class_simple[:-5] if class_simple.endswith("Table") else class_simple
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    return snake


def loader_class_candidates(method_name: str, arg: str,
                            caller_root_ns: str) -> list[tuple[str, str]]:
    """Candidate FQCNs for a loader call with a literal string argument.

    Returns [(rule, fqcn)] most-specific first: the named plugin, then the
    caller's own root namespace, then App, then the Cake framework namespace.
    """
    key = method_name.lower()
    if key not in CAKE_LOADERS:
        return []
    sub_ns, suffix = CAKE_LOADERS[key]
    plugin, name = split_plugin(arg)
    if not name or not re.match(r"^[A-Za-z_]\w*$", name):
        return []
    cls = name if name.endswith(suffix) else f"{name}{suffix}"
    rule = key
    roots: list[str] = []
    if plugin:
        roots.append(plugin)
    else:
        if caller_root_ns and caller_root_ns not in ("App", "Cake"):
            roots.append(caller_root_ns)
        roots.append("App")
        roots.append("Cake")
    out: list[tuple[str, str]] = []
    for root in roots:
        ns = CAKE_CORE_SUBNS[key] if root == "Cake" and key in CAKE_CORE_SUBNS else sub_ns
        out.append((rule, f"{root}\\{ns}\\{cls}"))
    return out


def locator_get_candidates(site_receiver: str, arg: str,
                           caller_root_ns: str) -> list[tuple[str, str]]:
    """`getTableLocator()->get('Users')` / `TableRegistry::...->get('X.Y')`."""
    if not _LOCATOR_RECEIVER.search(site_receiver or ""):
        return []
    return [(f"locator_get", fqcn)
            for _, fqcn in loader_class_candidates("fetchTable", arg, caller_root_ns)]


def entity_class_for_table(table_fqcn: str) -> str | None:
    """`Billing\\Model\\Table\\InvoicesTable` → `Billing\\Model\\Entity\\Invoice`."""
    m = re.match(r"^(.*)\\Model\\Table\\(\w+)Table$", table_fqcn)
    if not m:
        return None
    root, name = m.group(1), m.group(2)
    return f"{root}\\Model\\Entity\\{singularize(name)}"


# Table methods whose return value is (an) entity of that table.
TABLE_ENTITY_RETURNING = {
    "get", "first", "firstorfail", "newentity", "newemptyentity",
    "patchentity", "findorcreate",
}

# Controller base classes that activate the default-table magic property.
CONTROLLER_SUFFIX = "Controller"


def default_table_fqcn(controller_fqcn: str) -> str | None:
    """`Billing\\Controller\\InvoicesController` → `Billing\\Model\\Table\\InvoicesTable`."""
    m = re.match(r"^(.*)\\Controller\\(?:\w+\\)*(\w+)Controller$", controller_fqcn)
    if not m:
        return None
    root, name = m.group(1), m.group(2)
    if not name or name in ("App", "Error"):
        return None
    return f"{root}\\Model\\Table\\{name}Table"


def virtual_getter_name(prop: str) -> str:
    """Cake entity virtual field: `full_name` → `_getFullName`."""
    camel = "".join(p[:1].upper() + p[1:] for p in prop.split("_") if p)
    return f"_get{camel}"
