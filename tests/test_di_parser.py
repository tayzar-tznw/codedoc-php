"""DI container wiring extraction (di_parser)."""

import os

from graph_generator.di_parser import extract_di_bindings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _services(body):
    return (f"<?php\nnamespace App;\nuse Cake\\Core\\ContainerInterface;\n"
            f"class Application {{\n"
            f"    public function services(ContainerInterface $c): void {{\n{body}\n    }}\n}}\n")


def test_real_cakephp_application():
    src = open(os.path.join(REPO_ROOT, "test_codes", "php_cakephp",
                            "src", "Application.php"), encoding="utf-8").read()
    binds = extract_di_bindings("Application.php", src)
    assert {"kind": "inject", "source": "\\App\\Command\\SendInvoicesCommand",
            "target": "\\App\\Service\\InvoiceMailer", "line": 110} in binds
    # add(InvoiceMailer::class) alone is a registration, not an edge
    assert all(not (b["kind"] == "inject" and b["target"] == "\\App\\Service\\InvoiceMailer"
                    and b["source"] == "\\App\\Service\\InvoiceMailer") for b in binds)


def test_interface_bind():
    b = extract_di_bindings("x.php", _services(
        "$c->add(\\App\\Gateway::class, \\App\\StripeGateway::class);"))
    assert b == [{"kind": "bind", "source": "\\App\\Gateway",
                  "target": "\\App\\StripeGateway", "line": 6}]


def test_chained_inject():
    b = extract_di_bindings("x.php", _services(
        "$c->add(\\App\\Svc::class)->addArgument(\\App\\A::class)->addArgument(\\App\\B::class);"))
    kinds = [(x["kind"], x["source"], x["target"]) for x in b]
    assert ("inject", "\\App\\Svc", "\\App\\A") in kinds
    assert ("inject", "\\App\\Svc", "\\App\\B") in kinds


def test_addshared_and_extend():
    b = extract_di_bindings("x.php", _services(
        "$c->addShared(\\App\\I::class, \\App\\C::class);\n"
        "        $c->extend(\\App\\Svc::class)->addArgument(\\App\\Dep::class);"))
    kinds = {(x["kind"], x["source"], x["target"]) for x in b}
    assert ("bind", "\\App\\I", "\\App\\C") in kinds
    assert ("inject", "\\App\\Svc", "\\App\\Dep") in kinds


def test_non_class_args_skipped():
    # string keys / scalar args produce no edges
    b = extract_di_bindings("x.php", _services(
        "$c->add('apiKey', 'value');\n"
        "        $c->add(\\App\\Cmd::class)->addArgument('scalar');"))
    assert b == []


def test_scoped_to_services_only():
    # an ->add() outside a services() method must be ignored
    src = ("<?php\nnamespace App;\nclass Registry {\n"
           "    public function build(): void {\n"
           "        $col->add(\\App\\NotWiring::class, \\App\\Other::class);\n"
           "    }\n}\n")
    assert extract_di_bindings("x.php", src) == []


def test_serviceprovider_services_recognized():
    src = ("<?php\nnamespace App;\nuse Cake\\Core\\ContainerInterface;\n"
           "class BillingServiceProvider {\n"
           "    public function services(ContainerInterface $container): void {\n"
           "        $container->add(\\App\\Pay::class, \\App\\Stripe::class);\n"
           "    }\n}\n")
    b = extract_di_bindings("x.php", src)
    assert ("bind", "\\App\\Pay", "\\App\\Stripe") in {(x["kind"], x["source"], x["target"]) for x in b}


def test_non_php_and_garbage():
    assert extract_di_bindings("x.py", "print(1)") == []
    assert extract_di_bindings("x.php", "<?php // nothing") == []
