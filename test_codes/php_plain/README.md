# php_plain — core-language resolution fixture

Framework-free composer-style project. The `vendor/` tree is **hand-authored** (no `composer install`, no symlinks): `acme/reporting` and `globex/reporting` are twin packages that both define `Report::generate()`. Autoload maps are truthful (verified by `composer dump-autoload -o` and the repo validator); `vendor/composer/installed.json` is provided in composer-2 format for resolvers that read it.

Ground truth: `ground_truth.json` (49 cases). Scenario index — every directory is one scenario, named `S<nn>_<Topic>`:

| Dir | Tests | Why syntax-only parsing fails |
|---|---|---|
| `S01_Aliases` | `use`, `use ... as`, inline FQCN, group-use | The same short name `Report` means two different packages depending on import context; `CsvExporter` names a class that doesn't exist. |
| `S02_TypedReceivers` | same method on unrelated classes | `send()` has two declarations; picking one requires receiver types from constructor promotion, param hints, or `@var` docblocks. |
| `S03_VendorTwins` | **headline**: identical class *and* method across packages | `AcmeReportRunner` and `GlobexReportRunner` differ only in one `use` line; their call sites are byte-identical. |
| `S04_Inheritance` | 3-level chain, `parent::`, overrides | `parent::describe()` resolves to the *grandparent* (the parent doesn't override); nearest-override selection needed for `format()`. |
| `S05_InterfaceDispatch` | interface-typed calls | The only static declaration is the interface method; concrete targets are candidates (or flow-determined). |
| `S06_Traits` | trait methods, `insteadof`, `as` | Method bodies live outside the receiver class; `bonjour()` is declared nowhere — it's an alias. Runtime-verified: `hello()`='good day', `bonjour()`='hey'. |
| `S07_LateStaticBinding` | `new static()`, `static::` vs `self::` | One token (`static` vs `self`) flips the target; `static::name()` in the base is genuinely ambiguous without a call context. |
| `S08_Magic` | `__call`, `__callStatic`, `__get` | Calls route through magic to another class; the control case (`stop()`) must NOT route through magic. |
| `S12_Callables` | `[$obj,'m']`, `'FQCN::m'`, first-class `(...)` | Method references hide inside strings and array literals. |
| `S13_Functions` | namespaced vs global function fallback | Unqualified calls prefer the current namespace, then fall back to *global* — never to parent namespaces (`Sub/OtherNsConsumer`). |
| `S14_ConditionalTypes` | ternary/match receivers | Two unrelated `write()` declarations are both correct answers → `AMBIGUOUS` + candidates. |
| `S15_DynamicNames` | `new $class` from string concat | Negative control: statically unresolvable → `DYNAMIC`. |
| `S16_StaticVsInstance` | `x->now()` vs `X::now()` vs `$x::now()` | Same bare name, different classes and call styles, including static-through-instance. |

Notes: files carry no explanatory comments by design (the one docblock in `S02` *is* the scenario); intent lives here and in `why_hard`. `S09–S11` were folded into the CakePHP fixture's framework scenarios — the gap in numbering is intentional.
