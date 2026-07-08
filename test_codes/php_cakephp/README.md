# php_cakephp — framework-magic + vendor-scale fixture

Real CakePHP **5.3.6** application (scaffolded with `composer create-project cakephp/app:~5.3`, lock committed) plus authored code that recreates every resolution challenge CakePHP throws at a static analyzer. The ~4.5k-file `vendor/` tree is deliberate: cases whose answers live *inside* vendor measure whether a resolver survives name pollution at real scale. Regenerate vendor with `composer install` (see `../README.md`).

Ground truth: `ground_truth.json` (41 cases). Scenario index:

| Category | Sites | What it tests |
|---|---|---|
| `plugin-twins` (C01) | `PaymentRunner`, `DispatchRunner`, `OrdersController` | `Billing\Service\Gateway::charge()` vs `Shipping\Service\Gateway::charge()` — identical FQCN tails in two local plugins; byte-identical call lines. |
| `app-vendor-collision` (C02) | `AppUtilityConsumer` vs `VendorUtilityConsumer` | `App\Utility\Text::slug()` shadows the real `Cake\Utility\Text::slug()` with the same signature (likewise `Hash::get`, `Http\Client::get`). The two consumer files have byte-identical call lines; only imports differ. Runtime proof: app slug = `hello-world`, vendor slug = `Hello-World`. |
| `string-conventions` (C03) | controllers, tables, `ReportService`, template | `fetchTable('Articles')` → `ArticlesTable`, plugin-dot `fetchTable('Billing.Invoices')`, `loadComponent('Flash')` (vendor) / `('Billing.Payment')` (plugin), `addBehavior('Timestamp')`/`('Billing.Audit')`, `TableRegistry::getTableLocator()->get('Articles')`, `$this->Html->link()` in a template. Classes named only by strings + conventions. |
| `behavior-mixins` (C04) | `ArticlesController::edit` | `$articles->auditTrail($id)` exists on no Table — mixed in by the `Billing.Audit` behavior via `Table::__call`; `$this->Payment` materializes via `Controller::__get`. |
| `magic-finders` (C05) | `ArticlesController::view`, `ReportService` | `findBySlug()` / `findByEmail()` are declared nowhere; `Table::__call` parses the names at runtime. |
| `entity-virtuals` (C06) | template, `ReportService` | `$article->author_name` → `_getAuthorName()`, `$invoice->total_label` → plugin entity accessor, via `EntityTrait::__get`. |
| `fluent-chains` (C07) | `ArticlesController::index`, `ReportService` | `find()->where()->orderBy()->first()` — each link's declaration lives in a different vendor file (`ORM\Table`, `Database\Query`, `ORM\Query\SelectQuery`); receivers are return types, never variables. |
| `framework-callbacks` (C08) | `ArticlesTable`, plugin `InvoicesTable` | `beforeSave`/`afterSave` have **no call sites** — the event manager invokes them by convention during `save()`. |
| `app-to-vendor` (C09) | controller, command | `$this->request->getQuery()`, `Configure::read()`, `$this->viewBuilder()` — plain app→vendor hops, one defined in a vendor *trait*. |
| `needle-in-haystack` (C10) | `ArticlesController` | `get`/`set`/`save`/`all`/`first` — names with 100+ vendor declarations; includes `$article->set()` (EntityTrait) vs `$this->set()` (ViewVarsTrait) in the same controller. |
| `dependency-injection` (C11) | `SendInvoicesCommand`, `Application::services` | Constructor-promoted `InvoiceMailer` bound in the DI container; the wiring site and call site are different files. |

Authored/modified surface (everything else is stock scaffold): `src/Utility/`, `src/Http/Client.php`, `src/Service/`, `src/Controller/{Articles,Orders}Controller.php`, `src/Model/`, `src/Command/`, `src/Application.php` (services), `templates/Articles/index.php`, `config/plugins.php`, `config/app_local.example.php` (sqlite), `config/Migrations/` (Phinx: users, articles), `composer.json` (plugin PSR-4), `plugins/Billing/` (incl. its own `config/Migrations/` for invoices), `plugins/Shipping/`.

The migrations mirror the fields the fixture code actually touches (`articles.slug`/`published`/`author_first`/`author_last`, `users.email` behind `findByEmail`, `invoices.total` behind `_getTotalLabel`), so schema-aware tooling can join tables ↔ Table classes ↔ entities coherently.

---

*Stock skeleton docs: this project was created from [cakephp/app](https://github.com/cakephp/app) 5.x. To serve it locally: `bin/cake server -p 8765` (not needed for the fixture — nothing here requires execution).*
