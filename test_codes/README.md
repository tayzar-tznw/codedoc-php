# PHP semantic-resolution test fixtures

Two PHP codebases purpose-built to test resolution that **syntax-only parsing cannot do**: same method names across packages, identical class names in different namespaces/plugins, framework string conventions, and magic dispatch. They are the evaluation target for PHP entity extraction and the future Claude-Code-LSP-like "where does this method actually come from" feature, and they include a real thousands-of-files composer vendor tree for testing vendor handling at realistic scale.

| Fixture | What it is | Cases | Committed |
|---|---|---|---|
| `php_plain/` | Framework-free composer-style project; hand-authored `vendor/` with two packages that deliberately share class+method names | 49 | fully |
| `php_cakephp/` | Real CakePHP **5.3.6** app (`composer create-project cakephp/app:~5.3`) + two local plugins with colliding `Service\Gateway::charge()` + app classes shadowing real vendor classes + Phinx migrations | 41 | app code + lock; `vendor/`, `tmp/`, `logs/` stay local |

## Ground truth

Each fixture carries a `ground_truth.json`: one entry per tricky reference site with `file`/`line`/`expr` (+ `occurrence` for repeated snippets), the `expected` resolution (FQCN::method, or `AMBIGUOUS`/`DYNAMIC` with `candidates`), the `defined_in` file, magic hops (`syntactic_target`), the runtime `receiver` where it differs from the declaring class, `answer_location` (app / plugin / vendor), and a one-line `why_hard`. Line numbers are exact for authored files and frozen; vendor `defined_in` is path-only so composer.lock bumps don't invalidate entries. A resolver is correct on a case when it reports `expected` (for `AMBIGUOUS`/`DYNAMIC`, when it reports that status or the candidates set rather than picking one arbitrarily).

Validate the manifests any time with:

```bash
python3 test_codes/validate_ground_truth.py
```

Standalone stdlib script (no repo imports): checks schema, file existence, every `expr` at its exact `line`/`occurrence`, and PSR-4 truthfulness of authored code against the composer.json maps.

## Regenerating php_cakephp's vendor tree

```bash
cd test_codes/php_cakephp && composer install
```

`composer.lock` is committed, so this reproduces the exact tree (cakephp 5.3.6, ~4.5k vendor .php files). PHP ≥ 8.2 with `intl`, `mbstring`, `xml`, `sqlite3` extensions required. `config/app_local.php` is generated from `app_local.example.php` (sqlite path under `tmp/`; nothing in the fixtures ever needs a running DB — they only need to parse and autoload).

## Design invariants (keep these when extending)

- **Collisions are the point.** Twin classes/methods (`Report::generate`, `Gateway::charge`, app `Text::slug` vs vendor `Text::slug`) exist so that any name-only matcher provably picks a wrong or arbitrary target; `ground_truth.json` holds the per-site right answers. Don't "fix" duplicate names, and don't add inline comments to fixture code — intent lives in the READMEs and `why_hard` fields.
- **Byte-identical call sites.** Several file pairs differ only in their `use` statements; keep them textually identical at the call line when editing.
- **Frozen coordinates.** Any edit to a fixture file can shift `line` values — re-run the validator and update `ground_truth.json` in the same change.
- **Truthful autoload.** Every authored class must be PSR-4-reachable via the composer.json maps (validator-enforced); `php -l` must stay clean.

How the graph_generator pipeline scans or weighs these trees (vendor inclusion, skip rules, cost) is its own concern and evolves separately — fixtures make no assumptions about it.
